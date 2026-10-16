// Implementing the SideStore Anisette v3 protocol
//
// tvsign: the default server serves Windows anisette (Apple refuses Android anisette since
// 2026-10-12) with a fallback server, the client info AND the user agent of the server are kept,
// the identity lives in its own state file, a server answer of -45061 makes a new identity, Apple's
// synchronization request (hsc 434) is relayed to the server, a server that does not serve Windows
// is not used. See vendor/Impactor/PATCHES.md.
// The protocol handling follows isideload (https://github.com/nab138/isideload, MIT).

use std::{
    collections::HashMap,
    fs,
    io::Cursor,
    path::{Path, PathBuf},
    time::Duration,
};

use async_trait::async_trait;
use base64::Engine;
use base64::engine::general_purpose;
use chrono::{DateTime, SubsecRound, Utc};
use futures_util::{SinkExt, stream::StreamExt};
use log::{debug, info, warn};
use plist::{Data, Dictionary, Value};
use reqwest::{Client, ClientBuilder, RequestBuilder};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use sha2::{Digest, Sha256};
use std::fmt::Write;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use uuid::Uuid;

use crate::{AnisetteError, anisette_headers_provider::AnisetteHeadersProvider, server_order};

/// File holding the anisette identity (device identifier + provisioning). The name changed with the
/// Windows anisette: the Android identities kept in `state.plist` are useless to Apple now.
pub const STATE_FILE: &str = "anisette-state-windows.plist";
/// Where the Android-era identity was kept; removed once the new one is saved.
pub const LEGACY_STATE_FILE: &str = "state.plist";

const PROVISIONING_TIMEOUT: Duration = Duration::from_secs(60);

/// `X-Xcode-Version` of every request to GrandSlam (plume_core uses this value as well).
pub const XCODE_VERSION: &str = "27.0 (27A5218g)";
/// `X-Apple-App-Info` of every request to GrandSlam.
const APP_INFO: &str = "com.apple.gs.xcode.auth";
/// Apple's URL bag, which lists the two provisioning URLs.
const LOOKUP_URL: &str = "https://gsa.apple.com/grandslam/GsService2/lookup";
/// Client info of a Mac. The URL bag can depend on the client info it is asked with (isideload asks
/// for it with this one every time, because an Android one got a wrong bag): it is the second chance
/// when the bag asked with the server's own client info lacks the provisioning URLs.
const URL_BAG_CLIENT_INFO: &str =
    "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";
/// Environment variable that lets a server through whatever platform it announces (set it to `1`),
/// for the day Apple accepts more than Windows.
pub const ALLOW_ANY_PLATFORM_ENV: &str = "PLUMESIGN_ANISETTE_ALLOW_ANY_PLATFORM";

thread_local! {
    /// Where the calling thread asks for Apple's URL bag instead of `gsa.apple.com`: only the tests
    /// of the sign-in set it, to point the provisioning at a local imitation of Apple.
    static LOOKUP_URL_OVERRIDE: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

/// Point the URL bag lookup of the calling thread at `url` (`None`: Apple again). For tests only.
#[doc(hidden)]
pub fn set_lookup_url_for_tests(url: Option<String>) {
    LOOKUP_URL_OVERRIDE.with(|current| *current.borrow_mut() = url);
}

fn lookup_url() -> String {
    LOOKUP_URL_OVERRIDE
        .with(|url| url.borrow().clone())
        .unwrap_or_else(|| LOOKUP_URL.to_string())
}

fn is_truthy(value: &str) -> bool {
    matches!(value.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on")
}

fn env_flag(name: &str) -> bool {
    std::env::var(name).is_ok_and(|value| is_truthy(&value))
}

fn plist_to_string<T: serde::Serialize>(value: &T) -> Result<String, plist::Error> {
    plist_to_buf(value).map(|val| String::from_utf8(val).unwrap())
}

fn plist_to_buf<T: serde::Serialize>(value: &T) -> Result<Vec<u8>, plist::Error> {
    let mut buf: Vec<u8> = Vec::new();
    let writer = Cursor::new(&mut buf);
    plist::to_writer_xml(writer, &value)?;
    Ok(buf)
}

fn bin_serialize<S>(x: &[u8], s: S) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    s.serialize_bytes(x)
}

fn bin_serialize_opt<S>(x: &Option<Vec<u8>>, s: S) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    x.clone().map(|i| Data::new(i)).serialize(s)
}

fn bin_deserialize_opt<'de, D>(d: D) -> Result<Option<Vec<u8>>, D::Error>
where
    D: Deserializer<'de>,
{
    let s: Option<Data> = Deserialize::deserialize(d)?;
    Ok(s.map(|i| i.into()))
}

fn bin_deserialize_16<'de, D>(d: D) -> Result<[u8; 16], D::Error>
where
    D: Deserializer<'de>,
{
    let s: Data = Deserialize::deserialize(d)?;
    let s: Vec<u8> = s.into();
    s.try_into()
        .map_err(|_| serde::de::Error::custom("the device identifier must be 16 bytes"))
}

fn encode_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        write!(&mut s, "{:02x}", b).unwrap();
    }
    s
}
fn base64_encode(data: &[u8]) -> String {
    general_purpose::STANDARD.encode(data)
}

fn base64_decode(data: &str) -> Result<Vec<u8>, AnisetteError> {
    general_purpose::STANDARD
        .decode(data.trim())
        .map_err(|e| AnisetteError::ProvisioningFailed(format!("invalid base64 from the anisette server: {e}")))
}

/// First characters of an answer that is not what we expected: the `<title>` of an HTML error page
/// ("503 Service Temporarily Unavailable"), else the text itself on one line.
fn snippet(text: &str) -> String {
    let lower = text.to_ascii_lowercase();
    let title = lower.find("<title>").and_then(|a| {
        lower[a + 7..]
            .find("</title>")
            .map(|b| text[a + 7..a + 7 + b].trim().to_string())
    });
    title
        .unwrap_or_else(|| text.split_whitespace().collect::<Vec<_>>().join(" "))
        .chars()
        .take(200)
        .collect()
}

/// `https://host` -> `wss://host/v3/provisioning_session`, `http://host` -> `ws://...`.
fn websocket_url(server: &str) -> String {
    let url = format!("{}/v3/provisioning_session", server.trim_end_matches('/'));
    if let Some(rest) = url.strip_prefix("https://") {
        format!("wss://{rest}")
    } else if let Some(rest) = url.strip_prefix("http://") {
        format!("ws://{rest}")
    } else {
        url
    }
}

#[derive(Deserialize, Clone, Debug)]
pub struct AnisetteClientInfo {
    pub client_info: String,
    pub user_agent: String,
}

#[derive(Serialize, Deserialize)]
pub struct AnisetteState {
    #[serde(
        serialize_with = "bin_serialize",
        deserialize_with = "bin_deserialize_16"
    )]
    keychain_identifier: [u8; 16],
    // `default`: an unprovisioned identity is saved without this key (plist has no null)
    #[serde(
        default,
        serialize_with = "bin_serialize_opt",
        deserialize_with = "bin_deserialize_opt"
    )]
    adi_pb: Option<Vec<u8>>,
}

impl Default for AnisetteState {
    fn default() -> Self {
        AnisetteState {
            keychain_identifier: rand::random::<[u8; 16]>(),
            adi_pb: None,
        }
    }
}

impl AnisetteState {
    pub fn new() -> AnisetteState {
        AnisetteState::default()
    }

    pub fn is_provisioned(&self) -> bool {
        self.adi_pb.is_some()
    }

    fn md_lu(&self) -> [u8; 32] {
        let mut hasher = Sha256::new();
        hasher.update(&self.keychain_identifier);
        hasher.finalize().into()
    }

    fn device_id(&self) -> String {
        Uuid::from_bytes(self.keychain_identifier).to_string()
    }
}

/// Read the identity of this install; a missing or unreadable file means a new identity.
fn load_state(dir: &Path) -> AnisetteState {
    let path = dir.join(STATE_FILE);
    match plist::from_file::<_, AnisetteState>(&path) {
        Ok(state) => state,
        Err(e) => {
            if path.exists() {
                warn!("anisette state {} is unreadable ({e}): starting with a new identity", path.display());
            } else {
                info!("no anisette state yet: creating a new identity");
            }
            AnisetteState::new()
        }
    }
}

/// Write the identity atomically (a process killed in the middle never leaves half a file), and
/// drop the Android-era file that is useless now.
fn save_state(dir: &Path, state: &AnisetteState) -> Result<(), AnisetteError> {
    fs::create_dir_all(dir)?;
    let path = dir.join(STATE_FILE);
    let tmp = dir.join(format!("{STATE_FILE}.tmp"));
    plist::to_file_xml(&tmp, state)?;
    fs::rename(&tmp, &path)?;
    let legacy = dir.join(LEGACY_STATE_FILE);
    if legacy.exists() && fs::remove_file(&legacy).is_ok() {
        info!("removed the old anisette state {} (Android identity, refused by Apple)", legacy.display());
    }
    Ok(())
}

/// Forget the identity, device identifier included (see [`crate::reset_identity`]): the next request
/// makes a new one and provisions it. A server that answers "not provisioned" (-45061) is not
/// always willing to provision the same identifier again, a new one always starts clean.
pub fn reset_identity(dir: &Path) -> Result<(), AnisetteError> {
    for name in [STATE_FILE.to_string(), format!("{STATE_FILE}.tmp")] {
        match fs::remove_file(dir.join(&name)) {
            Ok(()) => info!("anisette identity {name} forgotten: a new one will be made"),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

pub struct AnisetteClient {
    client_info: AnisetteClientInfo,
    url: String,
}

#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct ProvisionBodyData {
    header: Dictionary,
    request: Dictionary,
}

#[derive(Debug)]
pub struct AnisetteData {
    machine_id: String,
    one_time_password: String,
    routing_info: String,
    device_description: String,
    local_user_id: String,
    device_unique_identifier: String,
}

impl AnisetteData {
    pub fn get_headers(&self, serial: String) -> HashMap<String, String> {
        let dt: DateTime<Utc> = Utc::now().round_subsecs(0);

        HashMap::from_iter(
            [
                (
                    "X-Apple-I-Client-Time".to_string(),
                    dt.format("%+").to_string().replace("+00:00", "Z"),
                ),
                ("X-Apple-I-SRL-NO".to_string(), serial),
                ("X-Apple-I-TimeZone".to_string(), "UTC".to_string()),
                ("X-Apple-Locale".to_string(), "en_US".to_string()),
                ("X-Apple-I-MD-RINFO".to_string(), self.routing_info.clone()),
                ("X-Apple-I-MD-LU".to_string(), self.local_user_id.clone()),
                (
                    "X-Mme-Device-Id".to_string(),
                    self.device_unique_identifier.clone(),
                ),
                ("X-Apple-I-MD".to_string(), self.one_time_password.clone()),
                ("X-Apple-I-MD-M".to_string(), self.machine_id.clone()),
                (
                    "X-Mme-Client-Info".to_string(),
                    self.device_description.clone(),
                ),
            ]
            .into_iter(),
        )
    }
}

/// Network delays: without them a silent connection blocks plumesign until tvsign kills it.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
/// Apple's root certificate, for the calls to gsa.apple.com of the provisioning.
const APPLE_ROOT: &[u8] = include_bytes!("./apple_root.der");

/// True when the user explicitly accepts an anisette server with a self-signed certificate.
fn anisette_insecure() -> bool {
    std::env::var("PLUMESIGN_ANISETTE_INSECURE")
        .map(|v| matches!(v.trim(), "1" | "true" | "yes"))
        .unwrap_or(false)
}

/// Client of the anisette server: certificates are verified, except on an explicit opt-in
/// (PLUMESIGN_ANISETTE_INSECURE=1) for a home server on self-signed https.
fn make_reqwest() -> Result<Client, AnisetteError> {
    let mut builder = ClientBuilder::new()
        .http1_title_case_headers()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT);
    if anisette_insecure() {
        builder = builder.danger_accept_invalid_certs(true);
    }
    Ok(builder.build()?)
}

/// Client of the calls to Apple (URL bag, start/finish provisioning): Apple's root added.
fn make_apple_reqwest() -> Result<Client, AnisetteError> {
    Ok(ClientBuilder::new()
        .http1_title_case_headers()
        .add_root_certificate(reqwest::Certificate::from_der(APPLE_ROOT)?)
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .build()?)
}

fn plist_string(dict: &Dictionary, key: &str) -> Result<String, AnisetteError> {
    dict.get(key)
        .and_then(Value::as_string)
        .map(str::to_string)
        .ok_or_else(|| AnisetteError::ProvisioningFailed(format!("Apple's answer has no {key}")))
}

/// Apple's answer to a provisioning request as a plist: an HTML error page (HTTP 503...) becomes
/// [`AnisetteError::AppleRefused`].
async fn apple_dict(resp: reqwest::Response) -> Result<Dictionary, AnisetteError> {
    let status = resp.status().as_u16();
    let text = resp.text().await?;
    match Value::from_reader(Cursor::new(text.as_bytes())) {
        Ok(Value::Dictionary(dict)) => Ok(dict),
        _ => Err(AnisetteError::AppleRefused {
            status,
            message: snippet(&text),
        }),
    }
}

/// The `Response` of a start/finish provisioning answer, refusing an error status.
fn provisioning_response(dict: &Dictionary) -> Result<&Dictionary, AnisetteError> {
    let response = dict
        .get("Response")
        .and_then(Value::as_dictionary)
        .ok_or_else(|| AnisetteError::ProvisioningFailed("Apple's answer has no Response".to_string()))?;
    if let Some(status) = response.get("Status").and_then(Value::as_dictionary) {
        let ec = status.get("ec").and_then(Value::as_signed_integer).unwrap_or(0);
        if ec != 0 {
            let em = status.get("em").and_then(Value::as_string).unwrap_or("");
            return Err(AnisetteError::ProvisioningFailed(format!(
                "Apple refused the provisioning (error {ec}: {em})"
            )));
        }
    }
    Ok(response)
}

impl AnisetteClient {
    pub async fn new(url: String) -> Result<AnisetteClient, AnisetteError> {
        let url = url.trim().trim_end_matches('/').to_string();
        let path = format!("{}/v3/client_info", url);
        let resp = make_reqwest()?.get(path).send().await?;
        let status = resp.status();
        let text = resp.text().await?;
        let client_info = serde_json::from_str::<AnisetteClientInfo>(&text).map_err(|_| {
            AnisetteError::ServerError(format!(
                "{url}/v3/client_info answered HTTP {} without client info: {}",
                status.as_u16(),
                snippet(&text)
            ))
        })?;
        if client_info.client_info.trim().is_empty() || client_info.user_agent.trim().is_empty() {
            return Err(AnisetteError::ServerError(format!(
                "{url}/v3/client_info has an empty client info or user agent"
            )));
        }
        Ok(AnisetteClient { client_info, url })
    }

    pub fn client_info(&self) -> &AnisetteClientInfo {
        &self.client_info
    }

    /// Apple only accepts Windows anisette now. A server that announces anything else (Android
    /// servers, which answer a Mac client info, included) is not used.
    pub fn serves_windows(&self) -> bool {
        self.client_info.client_info.contains("Windows")
    }

    /// A request to Apple's provisioning service: the headers GrandSlam gets from a client (client
    /// info and user agent of the server as a pair, Xcode version, app info, a connection that is
    /// not kept), plus the identity. `client_info` is the server's own, except for the second URL
    /// bag lookup.
    fn build_apple_request(
        &self,
        state: &AnisetteState,
        builder: RequestBuilder,
        client_info: &str,
    ) -> RequestBuilder {
        let dt: DateTime<Utc> = Utc::now().round_subsecs(0);

        builder
            .header("X-Mme-Client-Info", client_info)
            .header("User-Agent", &self.client_info.user_agent)
            .header("Content-Type", "text/x-xml-plist")
            .header("Accept", "text/x-xml-plist")
            .header("X-Xcode-Version", XCODE_VERSION)
            .header("X-Apple-App-Info", APP_INFO)
            .header("Connection", "close")
            .header("X-Apple-I-MD-LU", encode_hex(&state.md_lu()))
            .header("X-Mme-Device-Id", state.device_id())
            .header("X-Apple-I-Client-Time", dt.format("%+").to_string())
            .header("X-Apple-I-TimeZone", "UTC")
            .header("X-Apple-Locale", "en_US")
    }

    /// One-time-password headers. With `sim` (the `X-Apple-I-MD-DATA` of a GrandSlam answer with
    /// `hsc` 434) the server synchronizes the identity with Apple first.
    pub async fn get_headers(
        &self,
        state: &AnisetteState,
        sim: Option<&str>,
    ) -> Result<AnisetteData, AnisetteError> {
        let path = format!("{}/v3/get_headers", self.url);
        let http_client = make_reqwest()?;

        let mut body = serde_json::json!({
            "identifier": base64_encode(&state.keychain_identifier),
            "adi_pb": base64_encode(
                state
                    .adi_pb
                    .as_ref()
                    .ok_or(AnisetteError::AnisetteNotProvisioned)?,
            ),
        });
        if let Some(sim) = sim {
            body["sim"] = serde_json::Value::String(sim.to_string());
            body["device_id"] = serde_json::Value::String(state.device_id());
            body["md_lu"] = serde_json::Value::String(encode_hex(&state.md_lu()));
        }

        #[derive(Deserialize)]
        #[serde(tag = "result")]
        enum AnisetteHeaders {
            GetHeadersError {
                message: String,
            },
            Headers {
                #[serde(rename = "X-Apple-I-MD-M")]
                machine_id: String,
                #[serde(rename = "X-Apple-I-MD")]
                one_time_password: String,
                #[serde(rename = "X-Apple-I-MD-RINFO")]
                routing_info: String,
            },
        }

        // The status is not checked first: a server may report -45061 with an error status.
        let resp = http_client.post(path).json(&body).send().await?;
        let status = resp.status();
        let text = resp.text().await?;
        let headers = serde_json::from_str::<AnisetteHeaders>(&text).map_err(|_| {
            AnisetteError::ServerError(format!(
                "{}/v3/get_headers answered HTTP {} without headers: {}",
                self.url,
                status.as_u16(),
                snippet(&text)
            ))
        })?;
        match headers {
            AnisetteHeaders::GetHeadersError { message } => {
                if message.contains("-45061") {
                    Err(AnisetteError::AnisetteNotProvisioned)
                } else {
                    Err(AnisetteError::ServerError(message))
                }
            }
            AnisetteHeaders::Headers {
                machine_id,
                one_time_password,
                routing_info,
            } => Ok(AnisetteData {
                machine_id,
                one_time_password,
                routing_info,
                device_description: self.client_info.client_info.clone(),
                local_user_id: encode_hex(&state.md_lu()),
                device_unique_identifier: state.device_id(),
            }),
        }
    }

    /// The two provisioning URLs from Apple's URL bag, asked with `client_info`.
    async fn provisioning_urls(
        &self,
        state: &AnisetteState,
        client_info: &str,
    ) -> Result<(String, String), AnisetteError> {
        let http_client = make_apple_reqwest()?;
        let resp = self
            .build_apple_request(state, http_client.get(lookup_url()), client_info)
            .send()
            .await?;
        let bag = apple_dict(resp).await?;
        let urls = bag
            .get("urls")
            .and_then(Value::as_dictionary)
            .ok_or_else(|| AnisetteError::ProvisioningFailed("Apple's URL bag has no urls".to_string()))?;
        Ok((
            plist_string(urls, "midStartProvisioning")?,
            plist_string(urls, "midFinishProvisioning")?,
        ))
    }

    pub async fn provision(&self, state: &mut AnisetteState) -> Result<(), AnisetteError> {
        debug!("Provisioning Anisette");
        // The bag asked with the server's own client info (the one the rest of the sign-in uses) is
        // tried first; a bag without the provisioning URLs, or an HTML refusal, gets one more try
        // with the Mac client info that isideload sends every time.
        let (start_provisioning_url, end_provisioning_url) = match self
            .provisioning_urls(state, &self.client_info.client_info)
            .await
        {
            Ok(urls) => urls,
            Err(first @ (AnisetteError::ProvisioningFailed(_) | AnisetteError::AppleRefused { .. })) => {
                warn!("Apple's URL bag is not usable ({first}): asking again with a Mac client info");
                self.provisioning_urls(state, URL_BAG_CLIENT_INFO)
                    .await
                    .map_err(|second| {
                        AnisetteError::ProvisioningFailed(format!(
                            "{first}; asked again with a Mac client info: {second}"
                        ))
                    })?
            }
            Err(other) => return Err(other),
        };
        debug!(
            "Got provisioning urls: {} and {}",
            start_provisioning_url, end_provisioning_url
        );

        tokio::time::timeout(
            PROVISIONING_TIMEOUT,
            self.provisioning_session(state, &start_provisioning_url, &end_provisioning_url),
        )
        .await
        .map_err(|_| {
            AnisetteError::ProvisioningFailed(format!(
                "no answer from the anisette server after {} s",
                PROVISIONING_TIMEOUT.as_secs()
            ))
        })?
    }

    async fn provisioning_session(
        &self,
        state: &mut AnisetteState,
        start_provisioning_url: &str,
        end_provisioning_url: &str,
    ) -> Result<(), AnisetteError> {
        let provision_ws_url = websocket_url(&self.url);
        let (mut connection, _) = connect_async(&provision_ws_url).await?;

        #[derive(Deserialize)]
        #[serde(tag = "result")]
        enum ProvisionInput {
            GiveIdentifier,
            GiveStartProvisioningData,
            GiveEndProvisioningData {
                #[allow(dead_code)] // it's not even dead, rust just has problems
                cpim: String,
            },
            ProvisioningSuccess {
                #[allow(dead_code)] // it's not even dead, rust just has problems
                adi_pb: String,
            },
            Timeout,
            InvalidIdentifier,
            StartProvisioningError {
                message: String,
            },
            EndProvisioningError {
                message: String,
            },
        }

        loop {
            let data = match connection.next().await {
                Some(Ok(data)) => data,
                Some(Err(e)) => return Err(e.into()),
                None => {
                    return Err(AnisetteError::ProvisioningFailed(
                        "the anisette server closed the connection".to_string(),
                    ));
                }
            };
            if data.is_text() {
                let txt = data
                    .to_text()
                    .map_err(|e| AnisetteError::ProvisioningFailed(e.to_string()))?;
                let msg: ProvisionInput = serde_json::from_str(txt)?;
                match msg {
                    ProvisionInput::GiveIdentifier => {
                        #[derive(Serialize)]
                        struct Identifier {
                            identifier: String, // base64
                        }
                        let identifier = Identifier {
                            identifier: base64_encode(&state.keychain_identifier),
                        };
                        connection
                            .send(Message::Text(serde_json::to_string(&identifier)?.into()))
                            .await?;
                    }
                    ProvisionInput::GiveStartProvisioningData => {
                        let http_client = make_apple_reqwest()?;
                        let body_data = ProvisionBodyData {
                            header: Dictionary::new(),
                            request: Dictionary::new(),
                        };
                        let resp = self
                            .build_apple_request(
                                state,
                                http_client.post(start_provisioning_url),
                                &self.client_info.client_info,
                            )
                            .body(plist_to_string(&body_data)?)
                            .send()
                            .await?;
                        let protocol_val = apple_dict(resp).await?;
                        let spim = plist_string(provisioning_response(&protocol_val)?, "spim")?;

                        debug!("GiveStartProvisioningData");
                        #[derive(Serialize)]
                        struct Spim {
                            spim: String, // base64
                        }
                        connection
                            .send(Message::Text(serde_json::to_string(&Spim { spim })?.into()))
                            .await?;
                    }
                    ProvisionInput::GiveEndProvisioningData { cpim } => {
                        let http_client = make_apple_reqwest()?;
                        let body_data = ProvisionBodyData {
                            header: Dictionary::new(),
                            request: Dictionary::from_iter([("cpim", cpim)].into_iter()),
                        };
                        let resp = self
                            .build_apple_request(
                                state,
                                http_client.post(end_provisioning_url),
                                &self.client_info.client_info,
                            )
                            .body(plist_to_string(&body_data)?)
                            .send()
                            .await?;
                        let protocol_val = apple_dict(resp).await?;
                        let response = provisioning_response(&protocol_val)?;

                        debug!("GiveEndProvisioningData");

                        #[derive(Serialize)]
                        struct EndProvisioning {
                            ptm: String,
                            tk: String,
                        }
                        let end_provisioning = EndProvisioning {
                            ptm: plist_string(response, "ptm")?,
                            tk: plist_string(response, "tk")?,
                        };
                        connection
                            .send(Message::Text(
                                serde_json::to_string(&end_provisioning)?.into(),
                            ))
                            .await?;
                    }
                    ProvisionInput::ProvisioningSuccess { adi_pb } => {
                        debug!("ProvisioningSuccess");
                        state.adi_pb = Some(base64_decode(&adi_pb)?);
                        let _ = connection.close(None).await;
                        return Ok(());
                    }
                    ProvisionInput::Timeout => {
                        return Err(AnisetteError::ProvisioningFailed(
                            "the anisette server timed out".to_string(),
                        ));
                    }
                    ProvisionInput::InvalidIdentifier => {
                        return Err(AnisetteError::ProvisioningFailed(
                            "the anisette server refused the device identifier".to_string(),
                        ));
                    }
                    ProvisionInput::StartProvisioningError { message } => {
                        return Err(AnisetteError::ProvisioningFailed(format!(
                            "start of the provisioning refused: {message}"
                        )));
                    }
                    ProvisionInput::EndProvisioningError { message } => {
                        return Err(AnisetteError::ProvisioningFailed(format!(
                            "end of the provisioning refused: {message}"
                        )));
                    }
                }
            } else if data.is_close() {
                return Err(AnisetteError::ProvisioningFailed(
                    "the anisette server closed the connection before the end of the provisioning"
                        .to_string(),
                ));
            }
        }
    }
}

/// A failure that another server could fix (as opposed to a disk problem on this machine).
fn is_server_problem(error: &AnisetteError) -> bool {
    !matches!(
        error,
        AnisetteError::IOError(_) | AnisetteError::InvalidArgument(_)
    )
}

pub struct RemoteAnisetteProviderV3 {
    client_url: String,
    fallback_url: Option<String>,
    /// Server tried first (the one that served the previous data of the same sign-in).
    preferred_url: Option<String>,
    /// Server that produced the last headers.
    served_by: Option<String>,
    client_info: Option<AnisetteClientInfo>,
    pub state: Option<AnisetteState>,
    configuration_path: PathBuf,
    serial: String,
    new_identity: bool,
    allow_any_platform: bool,
}

impl RemoteAnisetteProviderV3 {
    pub fn new(
        url: String,
        configuration_path: PathBuf,
        serial: String,
    ) -> RemoteAnisetteProviderV3 {
        RemoteAnisetteProviderV3 {
            client_url: url,
            fallback_url: None,
            preferred_url: None,
            served_by: None,
            client_info: None,
            state: None,
            configuration_path,
            serial,
            new_identity: false,
            allow_any_platform: false,
        }
    }

    /// Server to use when the main one fails.
    pub fn with_fallback(mut self, fallback_url: Option<String>) -> RemoteAnisetteProviderV3 {
        self.fallback_url = fallback_url;
        self
    }

    /// Server to try first: a dead default server costs its connection timeout once per sign-in,
    /// not at every refresh of the headers.
    pub fn with_preferred(mut self, preferred_url: Option<String>) -> RemoteAnisetteProviderV3 {
        self.preferred_url = preferred_url;
        self
    }

    /// Use a server that does not announce Windows anisette (a diagnostic, to compare what Apple
    /// answers to each kind). The environment variable [`ALLOW_ANY_PLATFORM_ENV`] does the same.
    pub fn allow_any_platform(mut self, allow: bool) -> RemoteAnisetteProviderV3 {
        self.allow_any_platform = allow;
        self
    }

    /// Headers from one server: provisions the identity when needed, makes a new one when the
    /// server says it is not valid (-45061), synchronizes first when a `sim` is given.
    async fn headers_from(
        &mut self,
        server: &str,
        sim: Option<&str>,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        let client = AnisetteClient::new(server.to_string()).await?;
        if !client.serves_windows() {
            // Whatever such a server produces, Apple answers -80045 or an HTML 503: do not send it
            // there (every request to Apple counts against the account and the connection).
            let message = format!(
                "{server} announces `{}`: Apple only accepts Windows anisette since 2026-10-12 (use the default server, or one that serves Windows anisette)",
                client.client_info().client_info
            );
            if self.allow_any_platform || env_flag(ALLOW_ANY_PLATFORM_ENV) {
                warn!("{message}: used anyway");
            } else {
                return Err(AnisetteError::UnsupportedServer(message));
            }
        }

        if self.state.is_none() {
            self.state = Some(load_state(&self.configuration_path));
        }
        let state = self.state.as_mut().unwrap();
        if !state.is_provisioned() {
            info!("provisioning the anisette identity through {server}");
            client.provision(state).await?;
            self.new_identity = true;
            save_state(&self.configuration_path, state)?;
        }
        let data = match client.get_headers(state, sim).await {
            Ok(data) => data,
            Err(AnisetteError::AnisetteNotProvisioned) => {
                // As isideload does: a new device identifier and a full provisioning, not the same
                // identifier provisioned again (a server that lost track of it may refuse that).
                warn!("{server} answered -45061 (identity not provisioned): making a new identity");
                *state = AnisetteState::new();
                client.provision(state).await?;
                self.new_identity = true;
                save_state(&self.configuration_path, state)?;
                // The synchronization data of Apple (`sim`) belonged to the identity that is gone:
                // GrandSlam will ask the new one to synchronize by itself.
                client.get_headers(state, None).await?
            }
            Err(e) => return Err(e),
        };
        self.client_info = Some(client.client_info().clone());
        Ok(data.get_headers(self.serial.clone()))
    }

    /// [`Self::headers_from`] over the candidate servers: the next one is tried when a server
    /// cannot be reached or does not answer properly.
    async fn headers_with_failover(
        &mut self,
        sim: Option<&str>,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        self.new_identity = false;
        let order = server_order(
            &self.client_url,
            self.fallback_url.as_deref(),
            self.preferred_url.as_deref(),
        );
        let mut failures: Vec<String> = Vec::new();
        let mut first_error: Option<AnisetteError> = None;
        for server in order {
            match self.headers_from(&server, sim).await {
                Ok(headers) => {
                    self.served_by = Some(server);
                    return Ok(headers);
                }
                Err(e) if is_server_problem(&e) => {
                    warn!("anisette server {server} is not usable: {e}");
                    failures.push(format!("{server}: {e}"));
                    first_error.get_or_insert(e);
                }
                Err(e) => return Err(e),
            }
        }
        match (failures.len(), first_error) {
            (1, Some(error)) => Err(error),
            _ => Err(AnisetteError::AllServersFailed(failures.join(" | "))),
        }
    }
}

#[async_trait]
impl AnisetteHeadersProvider for RemoteAnisetteProviderV3 {
    async fn get_anisette_headers(
        &mut self,
        _skip_provisioning: bool,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        self.headers_with_failover(None).await
    }

    async fn resync_anisette_headers(
        &mut self,
        sim: &str,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        self.headers_with_failover(Some(sim)).await
    }

    fn user_agent(&self) -> Option<String> {
        self.client_info.as_ref().map(|info| info.user_agent.clone())
    }

    fn identity_is_new(&self) -> bool {
        self.new_identity
    }

    fn server_used(&self) -> Option<String> {
        self.served_by.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::anisette_headers_provider::AnisetteHeadersProvider;
    use crate::{AnisetteError, DEFAULT_ANISETTE_URL_V3};
    use log::info;

    #[tokio::test]
    async fn fetch_anisette_remote_v3() -> Result<(), AnisetteError> {
        crate::tests::init_logger();

        let mut provider = RemoteAnisetteProviderV3::new(
            DEFAULT_ANISETTE_URL_V3.to_string(),
            "anisette_test".into(),
            "0".to_string(),
        );
        info!(
            "Remote headers: {:?}",
            (&mut provider as &mut dyn AnisetteHeadersProvider)
                .get_authentication_headers()
                .await?
        );
        Ok(())
    }

    #[test]
    fn websocket_url_follows_the_scheme() {
        assert_eq!(websocket_url("https://ani.jkcoxson.com"), "wss://ani.jkcoxson.com/v3/provisioning_session");
        assert_eq!(websocket_url("https://ani.jkcoxson.com/"), "wss://ani.jkcoxson.com/v3/provisioning_session");
        assert_eq!(websocket_url("http://192.168.1.5:6969"), "ws://192.168.1.5:6969/v3/provisioning_session");
    }

    #[test]
    fn snippet_prefers_the_title_of_an_html_page() {
        let page = "<html>\r\n<head><title>503 Service Temporarily Unavailable</title></head>\r\n<body></body></html>";
        assert_eq!(snippet(page), "503 Service Temporarily Unavailable");
        assert_eq!(snippet("  plain   text \n here "), "plain text here");
        assert_eq!(snippet(&"x".repeat(500)).len(), 200);
        assert_eq!(snippet(""), "");
    }

    #[test]
    fn disk_problems_do_not_trigger_the_fallback() {
        assert!(is_server_problem(&AnisetteError::ServerError("x".into())));
        assert!(is_server_problem(&AnisetteError::ProvisioningFailed("x".into())));
        assert!(is_server_problem(&AnisetteError::AppleRefused { status: 503, message: "x".into() }));
        assert!(!is_server_problem(&AnisetteError::IOError(std::io::Error::other("disk"))));
    }

    #[test]
    fn identity_survives_a_save_and_is_forgotten_on_demand() {
        let dir = std::env::temp_dir().join(format!("omnisette-test-{}", rand::random::<u64>()));
        fs::create_dir_all(&dir).unwrap();
        // an Android-era file is removed when the new state is saved
        fs::write(dir.join(LEGACY_STATE_FILE), b"old").unwrap();

        let mut state = AnisetteState::new();
        state.adi_pb = Some(vec![1, 2, 3]);
        save_state(&dir, &state).unwrap();
        assert!(dir.join(STATE_FILE).exists());
        assert!(!dir.join(LEGACY_STATE_FILE).exists());
        assert!(!dir.join(format!("{STATE_FILE}.tmp")).exists());

        let loaded = load_state(&dir);
        assert_eq!(loaded.keychain_identifier, state.keychain_identifier);
        assert_eq!(loaded.adi_pb, Some(vec![1, 2, 3]));

        // forgetting the identity takes the identifier with it, and does not mind a second time
        reset_identity(&dir).unwrap();
        assert!(!dir.join(STATE_FILE).exists());
        let loaded = load_state(&dir);
        assert_ne!(loaded.keychain_identifier, state.keychain_identifier);
        assert!(!loaded.is_provisioned());
        reset_identity(&dir).unwrap();
        reset_identity(&dir.join("never-existed")).unwrap();
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn unreadable_state_means_a_new_identity() {
        let dir = std::env::temp_dir().join(format!("omnisette-test-{}", rand::random::<u64>()));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(STATE_FILE), b"not a plist").unwrap();
        assert!(!load_state(&dir).is_provisioned());
        // a 15-byte identifier is not an identifier: no panic, a new identity
        let bad = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>keychain_identifier</key><data>AQIDBAUGBwgJCgsMDQ4=</data></dict></plist>";
        fs::write(dir.join(STATE_FILE), bad).unwrap();
        assert!(!load_state(&dir).is_provisioned());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn device_headers_derive_from_the_identifier() {
        let state = AnisetteState {
            keychain_identifier: [7; 16],
            adi_pb: None,
        };
        assert_eq!(state.device_id(), "07070707-0707-0707-0707-070707070707");
        assert_eq!(encode_hex(&state.md_lu()).len(), 64);
        assert!(encode_hex(&state.md_lu()).chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn windows_servers_are_recognised_by_their_client_info() {
        let windows = AnisetteClient {
            client_info: AnisetteClientInfo {
                client_info: "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>".into(),
                user_agent: "akd/1.0 CFNetwork/808.1.4".into(),
            },
            url: "https://x".into(),
        };
        assert!(windows.serves_windows());
        let mac = AnisetteClient {
            client_info: AnisetteClientInfo {
                client_info: "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>".into(),
                user_agent: "akd/1.0 CFNetwork/808.1.4".into(),
            },
            url: "https://x".into(),
        };
        assert!(!mac.serves_windows());
    }

    #[test]
    fn provisioning_errors_from_apple_are_reported() {
        let mut ok = Dictionary::new();
        let mut response = Dictionary::new();
        response.insert("spim".into(), Value::String("abc".into()));
        ok.insert("Response".into(), Value::Dictionary(response));
        assert_eq!(plist_string(provisioning_response(&ok).unwrap(), "spim").unwrap(), "abc");
        assert!(plist_string(provisioning_response(&ok).unwrap(), "ptm").is_err());

        let mut status = Dictionary::new();
        status.insert("ec".into(), Value::Integer((-45061).into()));
        status.insert("em".into(), Value::String("nope".into()));
        let mut response = Dictionary::new();
        response.insert("Status".into(), Value::Dictionary(status));
        let mut refused = Dictionary::new();
        refused.insert("Response".into(), Value::Dictionary(response));
        let error = provisioning_response(&refused).unwrap_err().to_string();
        assert!(error.contains("-45061") && error.contains("nope"), "{error}");
        assert!(provisioning_response(&Dictionary::new()).is_err());
    }

    // ---- against a local server: the HTTP side of the provider (no Apple, no Internet) ----

    use std::io::{BufRead, BufReader, Read, Write as IoWrite};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex as StdMutex};

    type Seen = Arc<StdMutex<Vec<(String, String)>>>;

    const WINDOWS_INFO: &str = r#"{"client_info":"<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>","user_agent":"akd/1.0 CFNetwork/808.1.4"}"#;
    const HEADERS: &str = r#"{"result":"Headers","X-Apple-I-MD-M":"machine","X-Apple-I-MD":"otp","X-Apple-I-MD-RINFO":"17106176"}"#;

    /// Minimal HTTP server: `routes` are (path, status, body); every request is recorded
    /// as (path, body). Answers 404 to anything else.
    fn serve(routes: Vec<(&'static str, u16, &'static str)>) -> (String, Seen) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let seen: Seen = Arc::new(StdMutex::new(Vec::new()));
        let log = seen.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let routes = routes.clone();
                let log = log.clone();
                std::thread::spawn(move || {
                    let mut reader = BufReader::new(stream.try_clone().unwrap());
                    let mut request_line = String::new();
                    if reader.read_line(&mut request_line).is_err() {
                        return;
                    }
                    let path = request_line.split_whitespace().nth(1).unwrap_or("").to_string();
                    let mut length = 0usize;
                    loop {
                        let mut line = String::new();
                        if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                            break;
                        }
                        if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                            length = v.trim().parse().unwrap_or(0);
                        }
                    }
                    let mut body = vec![0u8; length];
                    let _ = reader.read_exact(&mut body);
                    log.lock().unwrap().push((path.clone(), String::from_utf8_lossy(&body).to_string()));
                    let (status, answer) = routes
                        .iter()
                        .find(|(p, _, _)| *p == path)
                        .map(|(_, s, b)| (*s, *b))
                        .unwrap_or((404, "404 page not found"));
                    let _ = write!(
                        stream,
                        "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{answer}",
                        answer.len()
                    );
                });
            }
        });
        (url, seen)
    }

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(future)
    }

    /// A directory holding an identity that is already provisioned (no Apple needed).
    fn provisioned_dir() -> (PathBuf, AnisetteState) {
        let dir = std::env::temp_dir().join(format!("omnisette-http-{}", rand::random::<u64>()));
        let state = AnisetteState {
            keychain_identifier: [9; 16],
            adi_pb: Some(vec![1, 2, 3]),
        };
        save_state(&dir, &state).unwrap();
        (dir, state)
    }

    fn count(seen: &Seen, path: &str) -> usize {
        seen.lock().unwrap().iter().filter(|(p, _)| p == path).count()
    }

    #[test]
    fn headers_carry_the_client_info_and_user_agent_of_the_server() {
        let (url, seen) = serve(vec![("/v3/client_info", 200, WINDOWS_INFO), ("/v3/get_headers", 200, HEADERS)]);
        let (dir, state) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let headers = run(provider.get_authentication_headers()).unwrap();

        assert_eq!(
            headers["X-Mme-Client-Info"],
            "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>"
        );
        assert_eq!(headers["X-Apple-I-MD"], "otp");
        assert_eq!(headers["X-Apple-I-MD-M"], "machine");
        assert_eq!(headers["X-Mme-Device-Id"], state.device_id());
        assert_eq!(provider.user_agent().as_deref(), Some("akd/1.0 CFNetwork/808.1.4"));
        assert!(!provider.identity_is_new());
        // a plain request has no synchronization fields
        let body: serde_json::Value = serde_json::from_str(
            &seen.lock().unwrap().iter().find(|(p, _)| p == "/v3/get_headers").unwrap().1,
        )
        .unwrap();
        assert!(body.get("sim").is_none() && body.get("md_lu").is_none());
        assert_eq!(body["adi_pb"], base64_encode(&[1, 2, 3]));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_synchronization_sends_the_sim_with_the_device() {
        let (url, seen) = serve(vec![("/v3/client_info", 200, WINDOWS_INFO), ("/v3/get_headers", 200, HEADERS)]);
        let (dir, state) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let headers = run(provider.resync_anisette_headers("AAAABBGFET7N")).unwrap();
        assert_eq!(headers["X-Apple-I-MD"], "otp");

        let body: serde_json::Value = serde_json::from_str(
            &seen.lock().unwrap().iter().find(|(p, _)| p == "/v3/get_headers").unwrap().1,
        )
        .unwrap();
        assert_eq!(body["sim"], "AAAABBGFET7N");
        assert_eq!(body["device_id"], state.device_id());
        assert_eq!(body["md_lu"], encode_hex(&state.md_lu()));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_dead_default_server_is_replaced_by_the_fallback() {
        let (primary, primary_seen) = serve(vec![]); // 404 everywhere, like a broken backend
        let (fallback, fallback_seen) =
            serve(vec![("/v3/client_info", 200, WINDOWS_INFO), ("/v3/get_headers", 200, HEADERS)]);
        let (dir, _) = provisioned_dir();
        let mut provider =
            RemoteAnisetteProviderV3::new(primary, dir.clone(), "0".to_string()).with_fallback(Some(fallback.clone()));
        let headers = run(provider.get_authentication_headers()).unwrap();
        assert_eq!(headers["X-Apple-I-MD"], "otp");
        assert_eq!(count(&primary_seen, "/v3/client_info"), 1);
        assert_eq!(count(&fallback_seen, "/v3/client_info"), 1);
        assert_eq!(count(&fallback_seen, "/v3/get_headers"), 1);
        assert_eq!(count(&primary_seen, "/v3/get_headers"), 0);
        // the server that worked is reported, to be tried first by the next refresh of the sign-in
        assert_eq!(provider.server_used().as_deref(), Some(fallback.as_str()));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_server_that_answers_html_is_not_usable() {
        let (url, _) = serve(vec![("/v3/client_info", 200, "<html><title>Just a moment...</title></html>")]);
        let (dir, _) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err().to_string();
        assert!(error.contains("client_info") && error.contains("Just a moment"), "{error}");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_custom_server_is_not_replaced_and_both_failures_are_reported() {
        let (primary, _) = serve(vec![]);
        let (fallback, _) = serve(vec![("/v3/client_info", 500, "boom")]);
        let (dir, _) = provisioned_dir();

        let mut alone = RemoteAnisetteProviderV3::new(primary.clone(), dir.clone(), "0".to_string());
        let error = run(alone.get_authentication_headers()).unwrap_err();
        assert!(matches!(error, AnisetteError::ServerError(_)), "{error}");
        assert!(!matches!(error, AnisetteError::AllServersFailed(_)));

        let mut both = RemoteAnisetteProviderV3::new(primary.clone(), dir.clone(), "0".to_string())
            .with_fallback(Some(fallback.clone()));
        let error = run(both.get_authentication_headers()).unwrap_err();
        let AnisetteError::AllServersFailed(text) = &error else {
            panic!("{error}")
        };
        assert!(text.contains(&primary) && text.contains(&fallback), "{text}");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn an_error_status_does_not_hide_the_message_of_the_server() {
        // -45061 would provision again (Apple needed): another message stays a server error
        let (url, _) = serve(vec![
            ("/v3/client_info", 200, WINDOWS_INFO),
            ("/v3/get_headers", 500, r#"{"result":"GetHeadersError","message":"ADI error -1: busy"}"#),
        ]);
        let (dir, _) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err();
        assert!(matches!(&error, AnisetteError::ServerError(m) if m.contains("busy")), "{error}");
        fs::remove_dir_all(dir).unwrap();
    }
}

/// The provisioning and the choice of a server against a local imitation of the anisette server
/// (HTTP and the provisioning socket) and of Apple's provisioning service: no Apple, no Internet.
#[cfg(test)]
mod provisioning_tests {
    use super::*;
    use crate::anisette_headers_provider::AnisetteHeadersProvider;
    use std::io::{BufRead, BufReader, Read, Write as IoWrite};
    use std::net::{TcpListener, TcpStream};
    use std::sync::{Arc, Mutex};
    use tokio_tungstenite::tungstenite;

    const WINDOWS: &str =
        "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>";
    const MAC: &str = "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";
    const USER_AGENT: &str = "akd/1.0 CFNetwork/808.1.4";
    const HEADERS: &str = r#"{"result":"Headers","X-Apple-I-MD-M":"machine","X-Apple-I-MD":"otp","X-Apple-I-MD-RINFO":"17106176"}"#;
    const NOT_PROVISIONED: &str = r#"{"result":"GetHeadersError","message":"ADI error -45061 (not provisioned)"}"#;

    #[derive(Clone, Debug)]
    struct Seen {
        path: String,
        headers: HashMap<String, String>,
        /// Names of the headers as they came (a header sent twice shows twice).
        header_names: Vec<String>,
        body: String,
    }

    /// What the imitation says.
    struct Script {
        client_info: &'static str,
        /// X-Mme-Client-Info values for which Apple's URL bag lists the provisioning URLs
        bag_lists_urls: fn(&str) -> bool,
        /// (HTTP status, JSON) of the n-th (from 0) /v3/get_headers
        get_headers: fn(usize) -> (u16, &'static str),
    }

    impl Script {
        fn windows() -> Script {
            Script {
                client_info: WINDOWS,
                bag_lists_urls: |_| true,
                get_headers: |_| (200, HEADERS),
            }
        }
    }

    struct Mock {
        url: String,
        seen: Arc<Mutex<Vec<Seen>>>,
        /// Identifiers (base64) given on the provisioning socket.
        identifiers: Arc<Mutex<Vec<String>>>,
    }

    impl Mock {
        fn requests(&self, path: &str) -> Vec<Seen> {
            self.seen.lock().unwrap().iter().filter(|r| r.path == path).cloned().collect()
        }

        fn lookup(&self) -> String {
            format!("{}/apple/lookup", self.url)
        }
    }

    fn serve(script: Script) -> Mock {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let mock = Mock {
            url: url.clone(),
            seen: Arc::new(Mutex::new(Vec::new())),
            identifiers: Arc::new(Mutex::new(Vec::new())),
        };
        let script = Arc::new(script);
        let (seen, identifiers) = (mock.seen.clone(), mock.identifiers.clone());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { continue };
                let (script, seen, identifiers, url) =
                    (script.clone(), seen.clone(), identifiers.clone(), url.clone());
                std::thread::spawn(move || handle(stream, &script, &seen, &identifiers, &url));
            }
        });
        mock
    }

    fn handle(
        mut stream: TcpStream,
        script: &Script,
        seen: &Mutex<Vec<Seen>>,
        identifiers: &Mutex<Vec<String>>,
        url: &str,
    ) {
        // the provisioning socket is left to tungstenite, request line included
        let mut start = [0u8; 40];
        let peeked = stream.peek(&mut start).unwrap_or(0);
        if start[..peeked].starts_with(b"GET /v3/provisioning_session") {
            provisioning_socket(stream, identifiers);
            return;
        }

        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut request_line = String::new();
        if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
            return;
        }
        let path = request_line.split_whitespace().nth(1).unwrap_or("").to_string();
        let mut headers = HashMap::new();
        let mut header_names = Vec::new();
        loop {
            let mut line = String::new();
            if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                break;
            }
            if let Some((name, value)) = line.split_once(':') {
                header_names.push(name.trim().to_ascii_lowercase());
                headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
            }
        }
        let length = headers.get("content-length").and_then(|v| v.parse().ok()).unwrap_or(0usize);
        let mut body = vec![0u8; length];
        let _ = reader.read_exact(&mut body);
        let body = String::from_utf8_lossy(&body).to_string();

        let earlier = seen.lock().unwrap().iter().filter(|r| r.path == path).count();
        let client_info = headers.get("x-mme-client-info").cloned().unwrap_or_default();
        seen.lock().unwrap().push(Seen { path: path.clone(), headers, header_names, body });

        let plist = |inner: &str| {
            format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict>{inner}</dict></plist>")
        };
        let (status, answer) = match path.as_str() {
            "/v3/client_info" => (
                200,
                format!(r#"{{"client_info":"{}","user_agent":"{USER_AGENT}"}}"#, script.client_info),
            ),
            "/v3/get_headers" => {
                let (status, json) = (script.get_headers)(earlier);
                (status, json.to_string())
            }
            "/apple/lookup" if (script.bag_lists_urls)(&client_info) => (
                200,
                plist(&format!(
                    "<key>urls</key><dict><key>midStartProvisioning</key><string>{url}/apple/start</string><key>midFinishProvisioning</key><string>{url}/apple/finish</string></dict>"
                )),
            ),
            "/apple/lookup" => (200, plist("<key>urls</key><dict></dict>")),
            "/apple/start" => (200, plist("<key>Response</key><dict><key>spim</key><string>c3BpbQ==</string></dict>")),
            "/apple/finish" => (
                200,
                plist("<key>Response</key><dict><key>ptm</key><string>cHRt</string><key>tk</key><string>dGs=</string></dict>"),
            ),
            _ => (404, "404 page not found".to_string()),
        };
        let _ = write!(
            stream,
            "HTTP/1.1 {status} X\r\nContent-Type: text/plain\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{answer}",
            answer.len()
        );
    }

    /// What the anisette server says on its provisioning socket, whatever Apple answers.
    fn provisioning_socket(stream: TcpStream, identifiers: &Mutex<Vec<String>>) {
        let text = |json: &str| tungstenite::Message::Text(json.to_string().into());
        let mut socket = tungstenite::accept(stream).unwrap();
        socket.send(text(r#"{"result":"GiveIdentifier"}"#)).unwrap();
        let reply: serde_json::Value = serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
        identifiers.lock().unwrap().push(reply["identifier"].as_str().unwrap().to_string());
        socket.send(text(r#"{"result":"GiveStartProvisioningData"}"#)).unwrap();
        let reply: serde_json::Value = serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
        assert_eq!(reply["spim"], "c3BpbQ==");
        socket.send(text(r#"{"result":"GiveEndProvisioningData","cpim":"Y3BpbQ=="}"#)).unwrap();
        let reply: serde_json::Value = serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
        assert_eq!((reply["ptm"].as_str(), reply["tk"].as_str()), (Some("cHRt"), Some("dGs=")));
        socket
            .send(text(&format!(
                r#"{{"result":"ProvisioningSuccess","adi_pb":"{}"}}"#,
                base64_encode(b"adi-provisioned")
            )))
            .unwrap();
        let _ = socket.read();
    }

    /// Points the URL bag lookup of this thread at the imitation of Apple.
    struct LookupOverride;

    impl LookupOverride {
        fn to(mock: &Mock) -> LookupOverride {
            set_lookup_url_for_tests(Some(mock.lookup()));
            LookupOverride
        }
    }

    impl Drop for LookupOverride {
        fn drop(&mut self) {
            set_lookup_url_for_tests(None);
        }
    }

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(future)
    }

    fn new_dir() -> PathBuf {
        std::env::temp_dir().join(format!("omnisette-provisioning-{}", rand::random::<u64>()))
    }

    /// A directory holding an identity that is provisioned already.
    fn provisioned_dir() -> (PathBuf, AnisetteState) {
        let dir = new_dir();
        let state = AnisetteState {
            keychain_identifier: [9; 16],
            adi_pb: Some(vec![1, 2, 3]),
        };
        save_state(&dir, &state).unwrap();
        (dir, state)
    }

    fn json(text: &str) -> serde_json::Value {
        serde_json::from_str(text).unwrap()
    }

    #[test]
    fn provisioning_requests_carry_the_headers_of_a_client() {
        let mock = serve(Script::windows());
        let _apple = LookupOverride::to(&mock);
        let dir = new_dir();
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        let headers = run(provider.get_authentication_headers()).unwrap();
        assert_eq!(headers["X-Apple-I-MD"], "otp");
        assert!(provider.identity_is_new());

        for path in ["/apple/lookup", "/apple/start", "/apple/finish"] {
            let requests = mock.requests(path);
            assert_eq!(requests.len(), 1, "{path}");
            let h = &requests[0].headers;
            // the pair Apple compares, the same set a sign-in sends, and the identity
            assert_eq!(h["x-mme-client-info"], WINDOWS, "{path}");
            assert_eq!(h["user-agent"], USER_AGENT, "{path}");
            assert_eq!(h["content-type"], "text/x-xml-plist", "{path}");
            assert_eq!(h["accept"], "text/x-xml-plist", "{path}");
            assert_eq!(h["x-xcode-version"], XCODE_VERSION, "{path}");
            assert_eq!(h["x-apple-app-info"], "com.apple.gs.xcode.auth", "{path}");
            assert_eq!(h["connection"], "close", "{path}");
            assert_eq!(h["x-apple-i-md-lu"].len(), 64, "{path}");
            assert_eq!(h["x-mme-device-id"].len(), 36, "{path}");
        }

        // the identity that went through the provisioning is the one that was saved
        let saved = load_state(&dir);
        assert!(saved.is_provisioned());
        assert_eq!(*mock.identifiers.lock().unwrap(), vec![base64_encode(&saved.keychain_identifier)]);
        assert_eq!(
            mock.requests("/apple/start")[0].headers["x-mme-device-id"],
            saved.device_id()
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_bag_without_the_urls_is_asked_again_with_a_mac_client_info() {
        let mock = serve(Script {
            bag_lists_urls: |client_info| client_info.contains("Mac15,7"),
            ..Script::windows()
        });
        let _apple = LookupOverride::to(&mock);
        let dir = new_dir();
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        run(provider.get_authentication_headers()).unwrap();

        let lookups = mock.requests("/apple/lookup");
        assert_eq!(lookups.len(), 2);
        assert_eq!(lookups[0].headers["x-mme-client-info"], WINDOWS);
        assert_eq!(lookups[1].headers["x-mme-client-info"], URL_BAG_CLIENT_INFO);
        // the Mac client info is for the bag only: the provisioning itself is a Windows one
        for path in ["/apple/start", "/apple/finish"] {
            assert_eq!(mock.requests(path)[0].headers["x-mme-client-info"], WINDOWS, "{path}");
        }
        // one client info header each time, not the server's and the Mac's together
        for lookup in &lookups {
            assert_eq!(lookup.header_names.iter().filter(|n| *n == "x-mme-client-info").count(), 1);
        }
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_bag_that_never_lists_the_urls_is_an_error_after_two_tries() {
        let mock = serve(Script {
            bag_lists_urls: |_| false,
            ..Script::windows()
        });
        let _apple = LookupOverride::to(&mock);
        let dir = new_dir();
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err().to_string();
        assert!(error.contains("midStartProvisioning") && error.contains("Mac client info"), "{error}");
        assert_eq!(mock.requests("/apple/lookup").len(), 2);
        assert!(mock.requests("/apple/start").is_empty());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_lost_the_identity_gets_a_new_one_without_the_old_sim() {
        let mock = serve(Script {
            get_headers: |n| if n == 0 { (500, NOT_PROVISIONED) } else { (200, HEADERS) },
            ..Script::windows()
        });
        let _apple = LookupOverride::to(&mock);
        let (dir, old) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        let headers = run(provider.resync_anisette_headers("SIM")).unwrap();
        assert_eq!(headers["X-Apple-I-MD"], "otp");

        let asked = mock.requests("/v3/get_headers");
        assert_eq!(asked.len(), 2);
        let (first, second) = (json(&asked[0].body), json(&asked[1].body));
        // first the old identity, with the synchronization data of Apple...
        assert_eq!(first["identifier"], base64_encode(&old.keychain_identifier));
        assert_eq!(first["sim"], "SIM");
        // ... then a new identity, provisioned from scratch, without them (they belonged to the old one)
        assert_ne!(second["identifier"], first["identifier"]);
        assert!(second.get("sim").is_none() && second.get("md_lu").is_none());
        assert_eq!(*mock.identifiers.lock().unwrap(), vec![second["identifier"].as_str().unwrap().to_string()]);
        assert_ne!(second["adi_pb"], first["adi_pb"]);
        assert!(provider.identity_is_new());

        let saved = load_state(&dir);
        assert_eq!(base64_encode(&saved.keychain_identifier), second["identifier"].as_str().unwrap());
        assert_eq!(saved.adi_pb, Some(b"adi-provisioned".to_vec()));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_keeps_saying_not_provisioned_is_asked_once_more_and_no_more() {
        let mock = serve(Script {
            get_headers: |_| (500, NOT_PROVISIONED),
            ..Script::windows()
        });
        let _apple = LookupOverride::to(&mock);
        let (dir, _) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err();
        assert!(matches!(error, AnisetteError::AnisetteNotProvisioned), "{error}");
        assert_eq!(mock.requests("/v3/get_headers").len(), 2);
        assert_eq!(mock.identifiers.lock().unwrap().len(), 1, "one provisioning, no loop");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_is_not_windows_is_not_used_and_nothing_is_sent_to_apple() {
        let mock = serve(Script {
            client_info: MAC,
            ..Script::windows()
        });
        let _apple = LookupOverride::to(&mock);
        let (dir, _) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err();
        assert!(matches!(error, AnisetteError::UnsupportedServer(_)), "{error}");
        let text = error.to_string();
        assert!(text.contains("only accepts Windows anisette") && text.contains("macOS"), "{text}");
        assert!(mock.requests("/v3/get_headers").is_empty());
        assert!(mock.requests("/apple/lookup").is_empty());

        // the diagnostic tool compares what Apple answers to each kind of server
        let mut anyway = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string())
            .allow_any_platform(true);
        let headers = run(anyway.get_authentication_headers()).unwrap();
        assert_eq!(headers["X-Mme-Client-Info"], MAC);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_is_not_windows_gives_way_to_the_fallback() {
        let android = serve(Script {
            client_info: MAC,
            ..Script::windows()
        });
        let windows = serve(Script::windows());
        let (dir, _) = provisioned_dir();
        let mut provider = RemoteAnisetteProviderV3::new(android.url.clone(), dir.clone(), "0".to_string())
            .with_fallback(Some(windows.url.clone()));
        run(provider.get_authentication_headers()).unwrap();
        assert_eq!(provider.server_used().as_deref(), Some(windows.url.as_str()));
        assert!(android.requests("/v3/get_headers").is_empty());
        assert_eq!(windows.requests("/v3/get_headers").len(), 1);

        // a sign-in that went through the fallback asks it first from then on
        let mut next = RemoteAnisetteProviderV3::new(android.url.clone(), dir.clone(), "0".to_string())
            .with_fallback(Some(windows.url.clone()))
            .with_preferred(provider.server_used());
        run(next.get_authentication_headers()).unwrap();
        assert_eq!(android.requests("/v3/client_info").len(), 1, "the first provider asked it, the next did not");
        assert_eq!(windows.requests("/v3/get_headers").len(), 2);

        // both unusable: one error that names them both
        let other = serve(Script {
            client_info: MAC,
            ..Script::windows()
        });
        let mut hopeless = RemoteAnisetteProviderV3::new(android.url.clone(), dir.clone(), "0".to_string())
            .with_fallback(Some(other.url.clone()));
        let error = run(hopeless.get_authentication_headers()).unwrap_err();
        let AnisetteError::AllServersFailed(text) = &error else {
            panic!("{error}")
        };
        assert!(text.contains(&android.url) && text.contains(&other.url), "{text}");
        assert!(text.contains("only accepts Windows anisette"), "{text}");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn the_override_flag_is_read_like_the_other_flags() {
        for yes in ["1", "true", "TRUE", " yes ", "on"] {
            assert!(is_truthy(yes), "{yes}");
        }
        for no in ["", "0", "false", "no", "off", "2"] {
            assert!(!is_truthy(no), "{no}");
        }
    }
}
