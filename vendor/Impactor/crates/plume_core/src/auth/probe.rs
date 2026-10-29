//! Diagnostic: what does GrandSlam answer to the SRP `init` request with the anisette data of a
//! given server? Used by the hidden `plumesign probe-gsa` command and to check, against the real
//! Apple servers, that a configuration is accepted (no account, no password: the Apple ID is a
//! throwaway address that does not exist, the `complete` step is never sent).

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::SystemTime;

use omnisette::{AnisetteConfiguration, ClientOverrides};
use omnisette::anisette_headers_provider::AnisetteHeadersProvider;
use omnisette::remote_anisette_v3::RemoteAnisetteProviderV3;
use sha2::Sha256;
use srp::groups::G2048;

use crate::Error;
use crate::auth::account::{resync_sim, snippet};
use crate::auth::anisette_data::{AnisetteData, DEFAULT_USER_AGENT};
use crate::auth::{InitRequest, InitRequestBody, RequestHeader, GSA_SERVICE, gsa_url};

/// What GrandSlam answered to one `init` request.
#[derive(Debug, Clone, Default)]
pub struct ProbeAttempt {
    pub label: String,
    pub http_status: u16,
    /// `ec` / `em` of the `Status` (or of the answer itself), when it is a plist.
    pub ec: Option<i64>,
    pub em: Option<String>,
    pub hsc: Option<i64>,
    /// The answer carries the SRP salt and the server's public value (`s`, `B`): the account exists
    /// and the configuration is accepted without reserve.
    pub has_salt_and_b: bool,
    pub asks_resync: bool,
    /// First characters of the body when it is not a plist (HTML page).
    pub body_snippet: Option<String>,
}

impl ProbeAttempt {
    /// An answer that is an account-level one: Apple accepted the anisette data and the client.
    pub fn accepted(&self) -> bool {
        self.http_status == 200
            && self.ec != Some(-80045)
            && (self.has_salt_and_b || self.ec.is_some() || self.asks_resync)
    }
}

#[derive(Debug, Clone, Default)]
pub struct ProbeReport {
    pub server: String,
    pub client_info: String,
    pub user_agent: String,
    pub user_agent_overridden: bool,
    /// The client info or the user agent sent is not the one the server announced (an override).
    pub pair_overridden: bool,
    pub identity_is_new: bool,
    /// Anisette headers sent (long values shortened).
    pub headers: Vec<(String, String)>,
    pub attempts: Vec<ProbeAttempt>,
}

fn shorten(value: &str) -> String {
    if value.chars().count() > 48 {
        format!("{}...({} chars)", value.chars().take(24).collect::<String>(), value.chars().count())
    } else {
        value.to_string()
    }
}

async fn init_once(
    client: &reqwest::Client,
    anisette: &AnisetteData,
    username: &str,
    label: &str,
) -> Result<(ProbeAttempt, Option<String>), Error> {
    let srp_client = srp::Client::<G2048, Sha256>::new_with_options(false);
    let a: Vec<u8> = (0..32).map(|_| rand::random::<u8>()).collect();
    let a_pub = srp_client.compute_public_ephemeral(&a);

    let packet = InitRequest {
        header: RequestHeader { version: "1.0.1".to_string() },
        request: InitRequestBody {
            a_pub: plist::Value::Data(a_pub),
            cpd: anisette.to_plist(true, false, false),
            operation: "init".to_string(),
            ps: vec!["s2k".to_string(), "s2k_fo".to_string()],
            username: username.to_string(),
        },
    };
    let mut buffer = Vec::new();
    plist::to_writer_xml(&mut buffer, &packet)?;

    // one single request, no retry: a probe must never insist
    let res = client
        .post(gsa_url(GSA_SERVICE))
        .headers(anisette.grandslam_headers(false)?)
        .body(buffer)
        .send()
        .await?;
    let http_status = res.status().as_u16();
    let text = res.text().await?;

    let mut attempt = ProbeAttempt { label: label.to_string(), http_status, ..Default::default() };
    let start = text.trim_start();
    if !start.starts_with("<?xml") && !start.starts_with("<plist") {
        attempt.body_snippet = Some(snippet(&text));
        return Ok((attempt, None));
    }
    let mut sim = None;
    let outer: plist::Dictionary = plist::from_bytes(text.as_bytes())?;
    if let Some(plist::Value::Dictionary(dict)) = outer.get("Response") {
        let status = dict.get("Status").and_then(plist::Value::as_dictionary).unwrap_or(dict);
        attempt.ec = status.get("ec").and_then(plist::Value::as_signed_integer);
        attempt.em = status.get("em").and_then(plist::Value::as_string).map(str::to_owned);
        attempt.hsc = status.get("hsc").and_then(plist::Value::as_signed_integer);
        attempt.has_salt_and_b = dict.get("s").is_some() && dict.get("B").is_some();
        sim = resync_sim(dict);
        attempt.asks_resync = sim.is_some();
    }
    Ok((attempt, sim))
}

/// Fetch the anisette data of `server` (no failover: the server is used as given, whatever it
/// announces, so that the answers Apple gives to different servers and configurations can be
/// compared) and send the SRP `init` request for a throwaway Apple ID. A first answer that asks to
/// synchronize the new identity (hsc 434) is followed once. At most two requests are sent to Apple.
///
/// `legacy_user_agent`: send the fixed macOS user agent of tvsign 1.0.0 instead of the one the
/// server reports. `overrides`: send this client info and user agent instead of the server's.
pub async fn probe_gsa_init(
    server: &str,
    state_dir: PathBuf,
    legacy_user_agent: Option<&str>,
    overrides: ClientOverrides,
) -> Result<ProbeReport, Error> {
    std::fs::create_dir_all(&state_dir)?;
    let pair_overridden = !overrides.is_empty();
    let mut provider = RemoteAnisetteProviderV3::new(server.to_string(), state_dir.clone(), "0".to_string())
        .with_overrides(overrides);
    let base_headers: HashMap<String, String> = provider.get_authentication_headers().await?;
    let server_ua = provider.user_agent().unwrap_or_else(|| DEFAULT_USER_AGENT.to_string());
    let user_agent = legacy_user_agent.map(str::to_owned).unwrap_or_else(|| server_ua.clone());

    let mut anisette = AnisetteData {
        base_headers,
        generated_at: SystemTime::now(),
        config: AnisetteConfiguration::default()
            .set_anisette_url_v3(server.to_string())
            .set_configuration_path(state_dir),
        user_agent: user_agent.clone(),
        fresh_identity: provider.identity_is_new(),
        server: provider.server_used(),
    };

    let mut report = ProbeReport {
        server: server.to_string(),
        client_info: anisette.client_info()?,
        user_agent,
        user_agent_overridden: legacy_user_agent.is_some(),
        pair_overridden,
        identity_is_new: anisette.fresh_identity,
        headers: {
            let mut v: Vec<_> = anisette
                .generate_headers(true, true, true)
                .into_iter()
                .map(|(k, v)| (k, shorten(&v)))
                .collect();
            v.sort();
            v
        },
        attempts: Vec::new(),
    };

    let client = crate::client()?;
    let username = format!("tvsign-probe-{:012x}@example.com", rand::random::<u64>() >> 16);
    let (first, sim) = init_once(&client, &anisette, &username, "init").await?;
    report.attempts.push(first);
    if let Some(sim) = sim {
        // Apple has never seen this identity: synchronize it (what a sign-in does), once
        let headers = provider.resync_anisette_headers(&sim).await?;
        anisette.base_headers = provider.normalize_headers(headers);
        anisette.generated_at = SystemTime::now();
        if legacy_user_agent.is_none() {
            anisette.user_agent = provider.user_agent().unwrap_or_else(|| server_ua.clone());
        }
        let synced = &anisette;
        let (second, _) = init_once(&client, synced, &username, "init after resync (hsc 434)").await?;
        report.attempts.push(second);
    }
    Ok(report)
}
