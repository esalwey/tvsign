// Implementing the SideStore Anisette v3 protocol
//
// tvsign: a list of servers with failover, whatever platform they announce (no platform is hard-coded:
// Apple's answer decides), the client info AND the user agent of the server are kept (or the pair the
// user overrides), one identity per server in its own state file, a server answer of -45061 makes a
// new identity, Apple's synchronization request (hsc 434) is relayed to the server, the server that
// worked last is remembered. See vendor/Impactor/PATCHES.md.
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

use crate::{
    AnisetteError, ClientOverrides, anisette_headers_provider::AnisetteHeadersProvider, normalize_server,
    same_server,
};

/// Folder (in the configuration folder) holding the anisette identities: one file per server,
/// `<server key>.plist` (device identifier + provisioning), and what was learnt of the servers.
/// An identity is provisioned by one server and is worth nothing to another, so servers never share.
pub const STATE_DIR: &str = "anisette";
/// Where the single identity of the earlier versions was kept (`state.plist`: Android, 1.0.0;
/// `anisette-state-windows.plist`: Windows, 1.0.1). Useless now; removed once an identity is saved.
pub const LEGACY_STATE_FILES: &[&str] = &["state.plist", "anisette-state-windows.plist"];
/// The server Apple accepted the data of last, in the state folder.
pub const LAST_SERVER_FILE: &str = "last-server.json";

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

/// File-name-safe key of a server: its host (with the port when it is not the default one, and the
/// path when it has one), lower case, anything but letters, digits, dots and dashes written as `_`.
/// Two addresses of one server (`https://x`, `https://X/`) have the same key.
pub fn server_key(url: &str) -> String {
    let url = url.trim();
    let (scheme, rest) = url.split_once("://").unwrap_or(("https", url));
    let rest = rest.split(['?', '#']).next().unwrap_or("");
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    let authority = authority.rsplit('@').next().unwrap_or("").to_ascii_lowercase();
    let authority = match (scheme.to_ascii_lowercase().as_str(), authority.as_str()) {
        ("https", a) if a.ends_with(":443") => a.trim_end_matches(":443").to_string(),
        ("http", a) if a.ends_with(":80") => a.trim_end_matches(":80").to_string(),
        _ => authority,
    };
    let mut key = authority.trim_end_matches('.').to_string();
    let path = path.trim_matches('/');
    if !path.is_empty() {
        key.push('/');
        key.push_str(&path.to_ascii_lowercase());
    }
    let safe: String = key
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '-' { c } else { '_' })
        .collect();
    let safe = safe.trim_matches('.').to_string();
    if safe.is_empty() {
        return "server".to_string();
    }
    // Windows reserves some names whatever the extension (`con.plist` is the console)
    let first = safe.split('.').next().unwrap_or("").to_ascii_lowercase();
    let numbered = |prefix: &str| {
        first.strip_prefix(prefix).is_some_and(|n| n.len() == 1 && n.as_bytes()[0].is_ascii_digit())
    };
    let safe = if matches!(first.as_str(), "con" | "prn" | "aux" | "nul") || numbered("com") || numbered("lpt") {
        format!("_{safe}")
    } else {
        safe
    };
    if safe.len() > 80 {
        // a file name that stays short, and still tells two long addresses apart
        let digest = hex::encode(Sha256::digest(key.as_bytes()));
        return format!("{}-{}", &safe[..64], &digest[..8]);
    }
    safe
}

/// Where the identity of `server` is kept.
pub fn state_path(dir: &Path, server: &str) -> PathBuf {
    dir.join(STATE_DIR).join(format!("{}.plist", server_key(server)))
}

/// Read the identity this install has with `server`; a missing or unreadable file means a new identity.
fn load_state(dir: &Path, server: &str) -> AnisetteState {
    let path = state_path(dir, server);
    match plist::from_file::<_, AnisetteState>(&path) {
        Ok(state) => state,
        Err(e) => {
            if path.exists() {
                warn!("anisette state {} is unreadable ({e}): starting with a new identity", path.display());
            } else {
                info!("no anisette identity with {server} yet: creating a new one");
            }
            AnisetteState::new()
        }
    }
}

/// Write the identity atomically (a process killed in the middle never leaves half a file), and
/// drop the files of the earlier versions, which are useless now.
fn save_state(dir: &Path, server: &str, state: &AnisetteState) -> Result<(), AnisetteError> {
    let path = state_path(dir, server);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("plist.tmp");
    plist::to_file_xml(&tmp, state)?;
    fs::rename(&tmp, &path)?;
    for name in LEGACY_STATE_FILES {
        let legacy = dir.join(name);
        if legacy.exists() && fs::remove_file(&legacy).is_ok() {
            info!("removed the anisette identity {} of an earlier version (one identity per server now)", legacy.display());
        }
    }
    Ok(())
}

/// Forget the identity with `server`, device identifier included (see [`crate::reset_identity`]): the
/// next request makes a new one and provisions it. A server that answers "not provisioned" (-45061)
/// is not always willing to provision the same identifier again, a new one always starts clean. The
/// identities with the other servers stay.
pub fn reset_identity(dir: &Path, server: &str) -> Result<(), AnisetteError> {
    let path = state_path(dir, server);
    for file in [path.clone(), path.with_extension("plist.tmp")] {
        match fs::remove_file(&file) {
            Ok(()) => info!("anisette identity with {server} forgotten: a new one will be made"),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

/// What is kept of the last server Apple accepted the data of.
#[derive(Serialize, Deserialize)]
struct LastServer {
    server: String,
    /// The servers that were configured at the time: the memory is valid for this very list, so that
    /// choosing another server (or a new version with other defaults) starts from the top again.
    servers: Vec<String>,
    at: String,
}

fn last_server_path(dir: &Path) -> PathBuf {
    dir.join(STATE_DIR).join(LAST_SERVER_FILE)
}

/// The server Apple accepted the data of last time, when `servers` is the list it was made for.
pub fn remembered_server(dir: &Path, servers: &[String]) -> Option<String> {
    if dir.as_os_str().is_empty() {
        return None;
    }
    let text = fs::read_to_string(last_server_path(dir)).ok()?;
    let last: LastServer = serde_json::from_str(&text).ok()?;
    let same_list = last.servers.len() == servers.len()
        && last.servers.iter().zip(servers).all(|(a, b)| same_server(a, b));
    if !same_list {
        return None;
    }
    // the entry of the list itself, however the file spelled it
    servers.iter().find(|s| same_server(s, &last.server)).cloned()
}

/// Keep `server` (one of `servers`) as the one to try first next time. Best effort.
pub fn remember_server(dir: &Path, servers: &[String], server: &str) {
    if dir.as_os_str().is_empty() {
        return;
    }
    let Some(known) = servers.iter().find(|s| same_server(s, server)) else {
        return;
    };
    if remembered_server(dir, servers).is_some_and(|remembered| same_server(&remembered, known)) {
        return;
    }
    let last = LastServer {
        server: known.clone(),
        servers: servers.to_vec(),
        at: Utc::now().to_rfc3339(),
    };
    let path = last_server_path(dir);
    let written = (|| -> Result<(), AnisetteError> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let tmp = path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_vec_pretty(&last)?)?;
        fs::rename(&tmp, &path)?;
        Ok(())
    })();
    match written {
        Ok(()) => info!("anisette server {server} remembered: Apple accepted its data"),
        Err(e) => debug!("could not remember the anisette server {server}: {e}"),
    }
}

pub struct AnisetteClient {
    /// What goes to Apple: the pair the server announces, or the one the user overrides it with.
    client_info: AnisetteClientInfo,
    /// What the server announced.
    announced: AnisetteClientInfo,
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

fn make_reqwest() -> Result<Client, AnisetteError> {
    Ok(ClientBuilder::new()
        .http1_title_case_headers()
        .danger_accept_invalid_certs(true) // TODO: pin the apple certificate
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
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
        Ok(AnisetteClient {
            announced: client_info.clone(),
            client_info,
            url,
        })
    }

    /// Send `overrides` to Apple instead of what the server announces (each of the two on its own).
    pub fn with_overrides(mut self, overrides: &ClientOverrides) -> AnisetteClient {
        if let Some(client_info) = &overrides.client_info {
            self.client_info.client_info = client_info.clone();
        }
        if let Some(user_agent) = &overrides.user_agent {
            self.client_info.user_agent = user_agent.clone();
        }
        self
    }

    /// The client info and user agent that go to Apple with this server's data.
    pub fn client_info(&self) -> &AnisetteClientInfo {
        &self.client_info
    }

    /// The client info and user agent the server announced (`/v3/client_info`).
    pub fn announced(&self) -> &AnisetteClientInfo {
        &self.announced
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
        let http_client = make_reqwest()?;
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
                        let http_client = make_reqwest()?;
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
                        let http_client = make_reqwest()?;
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

//// A failure that another server could fix (as opposed to a disk problem on this machine).
fn is_server_problem(error: &AnisetteError) -> bool {
    !matches!(
        error,
        AnisetteError::IOError(_) | AnisetteError::InvalidArgument(_)
    )
}

pub struct RemoteAnisetteProviderV3 {
    /// The servers to try, in this order.
    servers: Vec<String>,
    /// Server that produced the last headers.
    served_by: Option<String>,
    /// The client info and user agent that go to Apple with the last headers.
    client_info: Option<AnisetteClientInfo>,
    configuration_path: PathBuf,
    serial: String,
    new_identity: bool,
    overrides: ClientOverrides,
}

impl RemoteAnisetteProviderV3 {
    /// A provider that uses `url` alone (see [`Self::with_fallbacks`], [`Self::with_servers`]).
    pub fn new(
        url: String,
        configuration_path: PathBuf,
        serial: String,
    ) -> RemoteAnisetteProviderV3 {
        RemoteAnisetteProviderV3::with_servers(vec![url], configuration_path, serial)
    }

    /// A provider that tries these servers in this order: the next one is used when a server cannot
    /// be reached, does not answer properly, or cannot give usable data.
    pub fn with_servers(
        servers: Vec<String>,
        configuration_path: PathBuf,
        serial: String,
    ) -> RemoteAnisetteProviderV3 {
        RemoteAnisetteProviderV3 {
            servers: unique_servers(servers),
            served_by: None,
            client_info: None,
            configuration_path,
            serial,
            new_identity: false,
            overrides: ClientOverrides::default(),
        }
    }

    /// Servers to use after the others when those fail.
    pub fn with_fallbacks(mut self, fallbacks: Vec<String>) -> RemoteAnisetteProviderV3 {
        let mut servers = std::mem::take(&mut self.servers);
        servers.extend(fallbacks);
        self.servers = unique_servers(servers);
        self
    }

    /// Server to try first: a dead server costs its connection timeout once per sign-in, not at every
    /// refresh of the headers. Ignored when it is not one of the servers.
    pub fn with_preferred(mut self, preferred_url: Option<String>) -> RemoteAnisetteProviderV3 {
        if let Some(preferred) = preferred_url
            && let Some(position) = self.servers.iter().position(|s| same_server(s, &preferred))
            && position > 0
        {
            let server = self.servers.remove(position);
            self.servers.insert(0, server);
        }
        self
    }

    /// Send this client info and user agent to Apple instead of the ones the servers announce.
    pub fn with_overrides(mut self, overrides: ClientOverrides) -> RemoteAnisetteProviderV3 {
        self.overrides = overrides;
        self
    }

    /// Headers from one server: provisions the identity of this server when needed, makes a new one
    /// when the server says it is not valid (-45061), synchronizes first when a `sim` is given. The
    /// data of a server are sent with the client info and user agent it announces, whatever they say:
    /// Apple's answer decides whether they are good, not a list of platforms.
    async fn headers_from(
        &mut self,
        server: &str,
        sim: Option<&str>,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        let client = AnisetteClient::new(server.to_string())
            .await?
            .with_overrides(&self.overrides);
        let announced = client.announced();
        if self.overrides.is_empty() {
            info!(
                "anisette server {server} announces `{}` with user agent `{}`",
                announced.client_info, announced.user_agent
            );
        } else {
            info!(
                "anisette server {server} announces `{}` with user agent `{}`; sending `{}` with user agent `{}` instead (override)",
                announced.client_info,
                announced.user_agent,
                client.client_info().client_info,
                client.client_info().user_agent
            );
        }

        let mut provisioned_now = false;
        let mut state = load_state(&self.configuration_path, server);
        if !state.is_provisioned() {
            info!("provisioning the anisette identity of {server}");
            client.provision(&mut state).await?;
            provisioned_now = true;
            save_state(&self.configuration_path, server, &state)?;
        }
        let data = match client.get_headers(&state, sim).await {
            Ok(data) => data,
            Err(AnisetteError::AnisetteNotProvisioned) => {
                // As isideload does: a new device identifier and a full provisioning, not the same
                // identifier provisioned again (a server that lost track of it may refuse that).
                warn!("{server} answered -45061 (identity not provisioned): making a new identity");
                state = AnisetteState::new();
                client.provision(&mut state).await?;
                provisioned_now = true;
                save_state(&self.configuration_path, server, &state)?;
                // The synchronization data of Apple (`sim`) belonged to the identity that is gone:
                // GrandSlam will ask the new one to synchronize by itself.
                client.get_headers(&state, None).await?
            }
            Err(e) => return Err(e),
        };
        self.new_identity = provisioned_now;
        self.client_info = Some(client.client_info().clone());
        Ok(data.get_headers(self.serial.clone()))
    }

    /// [`Self::headers_from`] over the candidate servers: the next one is tried when a server
    /// cannot be reached, does not answer properly, or cannot give usable data (including a
    /// `-45061` that stays after one new identity).
    async fn headers_with_failover(
        &mut self,
        sim: Option<&str>,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        self.new_identity = false;
        if self.servers.is_empty() {
            return Err(AnisetteError::AllServersFailed(
                "no anisette server is left to try".to_string(),
            ));
        }
        let mut failures: Vec<String> = Vec::new();
        let mut first_error: Option<AnisetteError> = None;
        for server in self.servers.clone() {
            match self.headers_from(&server, sim).await {
                Ok(headers) => {
                    if !failures.is_empty() {
                        info!("anisette server {server} used after {} that failed", failures.len());
                    }
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

/// The servers, normalised, each once (the first mention wins).
fn unique_servers(servers: Vec<String>) -> Vec<String> {
    let mut unique: Vec<String> = Vec::new();
    for server in servers.iter().map(|s| normalize_server(s)) {
        if !unique.iter().any(|known| same_server(known, &server)) {
            unique.push(server);
        }
    }
    unique
}

#[async_trait]
impl AnisetteHeadersProvider for RemoteAnisetteProviderV3 {
    async fn get_anisette_headers(
        &mut self,
        _skip_provisioning: bool,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        self.headers_with_failover(None).await
    }

    /// The `sim` belongs to the identity of the server that produced the data Apple asked to
    /// synchronize: give this provider that server alone (`AnisetteConfiguration::only_server_v3`),
    /// another server could do nothing with it.
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
    use crate::{AnisetteConfiguration, AnisetteError, DEFAULT_ANISETTE_URL_V3};
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
        assert!(is_server_problem(&AnisetteError::AnisetteNotProvisioned));
        assert!(is_server_problem(&AnisetteError::AppleRefused { status: 503, message: "x".into() }));
        assert!(!is_server_problem(&AnisetteError::IOError(std::io::Error::other("disk"))));
    }

    fn temp_dir(prefix: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("{prefix}-{}", rand::random::<u64>()));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_server_key_is_a_safe_file_name_of_the_server() {
        assert_eq!(server_key("https://mac-ani.jkcoxson.com"), "mac-ani.jkcoxson.com");
        // one server, however its address is written
        for same in ["https://MAC-ani.jkcoxson.com/", "mac-ani.jkcoxson.com", "http://mac-ani.jkcoxson.com", "https://mac-ani.jkcoxson.com:443", "https://u:p@mac-ani.jkcoxson.com./?a#b"] {
            assert_eq!(server_key(same), "mac-ani.jkcoxson.com", "{same}");
        }
        // ... and two servers on one host do not share an identity
        assert_eq!(server_key("http://127.0.0.1:6969"), "127.0.0.1_6969");
        assert_ne!(server_key("http://127.0.0.1:6969"), server_key("http://127.0.0.1:6970"));
        assert_eq!(server_key("https://example.org/anisette/one"), "example.org_anisette_one");
        assert_ne!(server_key("https://example.org/one"), server_key("https://example.org/two"));
        // never a path, never an empty or reserved name, never too long
        for nasty in ["", "   ", "https://", "..", "../../etc/passwd", "https://a/b\\c", "https://[::1]:8080/", "https://con.example", "https://nul"] {
            let key = server_key(nasty);
            assert!(!key.is_empty() && !key.contains(['/', '\\', ':']), "{nasty:?} -> {key:?}");
            assert!(!key.starts_with('.') && !key.ends_with('.'), "{nasty:?} -> {key:?}");
            let first = key.split('.').next().unwrap().to_ascii_lowercase();
            assert!(!["con", "prn", "aux", "nul"].contains(&first.as_str()), "{nasty:?} -> {key:?}");
        }
        let long = format!("https://{}.example.org", "a".repeat(200));
        let key = server_key(&long);
        assert!(key.len() <= 80 + 9, "{key}");
        assert_ne!(key, server_key(&format!("https://{}.example.org", "b".repeat(200))));
    }

    #[test]
    fn identities_are_kept_per_server_and_forgotten_on_demand() {
        let dir = temp_dir("omnisette-test");
        // the identities of the earlier versions are removed once a new one is saved
        for legacy in LEGACY_STATE_FILES {
            fs::write(dir.join(legacy), b"old").unwrap();
        }
        let (a, b) = ("https://a.example", "https://b.example");
        let mut state_a = AnisetteState::new();
        state_a.adi_pb = Some(vec![1, 2, 3]);
        save_state(&dir, a, &state_a).unwrap();
        assert!(state_path(&dir, a).exists());
        assert!(state_path(&dir, a).starts_with(dir.join(STATE_DIR)));
        for legacy in LEGACY_STATE_FILES {
            assert!(!dir.join(legacy).exists(), "{legacy}");
        }
        assert!(!state_path(&dir, a).with_extension("plist.tmp").exists());

        let loaded = load_state(&dir, a);
        assert_eq!(loaded.keychain_identifier, state_a.keychain_identifier);
        assert_eq!(loaded.adi_pb, Some(vec![1, 2, 3]));
        // the same address written another way is the same server...
        assert_eq!(load_state(&dir, "HTTPS://A.example/").keychain_identifier, state_a.keychain_identifier);
        // ... another server shares nothing: it starts with an identity of its own
        let other = load_state(&dir, b);
        assert_ne!(other.keychain_identifier, state_a.keychain_identifier);
        assert!(!other.is_provisioned());

        let mut state_b = AnisetteState::new();
        state_b.adi_pb = Some(vec![9]);
        save_state(&dir, b, &state_b).unwrap();
        assert_ne!(state_path(&dir, a), state_path(&dir, b));

        // forgetting the identity with one server takes its identifier with it and leaves the other
        reset_identity(&dir, a).unwrap();
        assert!(!state_path(&dir, a).exists());
        assert!(state_path(&dir, b).exists());
        let loaded = load_state(&dir, a);
        assert_ne!(loaded.keychain_identifier, state_a.keychain_identifier);
        assert!(!loaded.is_provisioned());
        assert_eq!(load_state(&dir, b).adi_pb, Some(vec![9]));
        // and does not mind a second time, or a folder that was never there
        reset_identity(&dir, a).unwrap();
        reset_identity(&dir.join("never-existed"), a).unwrap();
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn unreadable_state_means_a_new_identity() {
        let dir = temp_dir("omnisette-test");
        let server = "https://a.example";
        fs::create_dir_all(dir.join(STATE_DIR)).unwrap();
        fs::write(state_path(&dir, server), b"not a plist").unwrap();
        assert!(!load_state(&dir, server).is_provisioned());
        // a 15-byte identifier is not an identifier: no panic, a new identity
        let bad = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>keychain_identifier</key><data>AQIDBAUGBwgJCgsMDQ4=</data></dict></plist>";
        fs::write(state_path(&dir, server), bad).unwrap();
        assert!(!load_state(&dir, server).is_provisioned());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_server_that_worked_is_remembered_for_the_list_it_was_made_for() {
        let dir = temp_dir("omnisette-test");
        let list = |servers: &[&str]| -> Vec<String> { servers.iter().map(|s| s.to_string()).collect() };
        let servers = list(&["https://a.example", "https://b.example", "https://c.example"]);
        assert_eq!(remembered_server(&dir, &servers), None);

        remember_server(&dir, &servers, "https://b.example");
        assert_eq!(remembered_server(&dir, &servers).as_deref(), Some("https://b.example"));
        let text = fs::read_to_string(dir.join(STATE_DIR).join(LAST_SERVER_FILE)).unwrap();
        assert!(text.contains("https://b.example") && text.contains("\"at\""), "{text}");

        // another list is another configuration (the user chose a server, a new version has other
        // defaults): the memory does not apply, and the first server of the list is tried first
        assert_eq!(remembered_server(&dir, &list(&["https://a.example", "https://c.example"])), None);
        assert_eq!(remembered_server(&dir, &list(&["https://b.example", "https://a.example", "https://c.example"])), None);
        // a server that is not in the list is not remembered
        remember_server(&dir, &servers, "https://z.example");
        assert_eq!(remembered_server(&dir, &servers).as_deref(), Some("https://b.example"));
        // the last one that worked replaces the one before
        remember_server(&dir, &servers, "https://C.example/");
        assert_eq!(remembered_server(&dir, &servers).as_deref(), Some("https://c.example"));
        // a file nobody can read is no memory
        fs::write(dir.join(STATE_DIR).join(LAST_SERVER_FILE), b"{").unwrap();
        assert_eq!(remembered_server(&dir, &servers), None);

        // no configuration folder (a bare configuration): no memory, and nothing written in the
        // current directory
        remember_server(Path::new(""), &servers, "https://a.example");
        assert_eq!(remembered_server(Path::new(""), &servers), None);
        assert!(!Path::new(STATE_DIR).join(LAST_SERVER_FILE).exists());
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_configuration_tries_the_remembered_server_first() {
        let dir = temp_dir("omnisette-test");
        let servers: Vec<String> = ["https://a.example", "https://b.example", "https://c.example"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let config = AnisetteConfiguration::default()
            .set_servers_v3(servers.clone())
            .set_configuration_path(dir.clone());
        assert_eq!(config.ordered_servers_v3(), servers);

        remember_server(&dir, &servers, "https://c.example");
        assert_eq!(config.ordered_servers_v3(), ["https://c.example", "https://a.example", "https://b.example"]);
        // what the sign-in itself has seen wins over the disk
        let seen = config.clone().set_preferred_url_v3(Some("https://b.example".into()));
        assert_eq!(seen.ordered_servers_v3()[0], "https://b.example");
        // the servers Apple refused are left out whatever the memory says
        let mut refused = config.clone();
        assert!(refused.refuse_server_v3("https://c.example"));
        assert_eq!(refused.ordered_servers_v3(), ["https://a.example", "https://b.example"]);
        // another list: the memory is not for it
        let own = config.clone().set_servers_v3(vec!["https://own.example".into(), "https://c.example".into()]);
        assert_eq!(own.ordered_servers_v3(), ["https://own.example", "https://c.example"]);
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
    fn overrides_replace_each_half_of_the_pair_on_its_own() {
        let announced = AnisetteClientInfo {
            client_info: "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>".into(),
            user_agent: "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0".into(),
        };
        let client = || AnisetteClient {
            client_info: announced.clone(),
            announced: announced.clone(),
            url: "https://x".into(),
        };
        let none = client().with_overrides(&ClientOverrides::default());
        assert_eq!(none.client_info().client_info, announced.client_info);
        assert_eq!(none.client_info().user_agent, announced.user_agent);

        let both = client().with_overrides(&ClientOverrides::new(Some("<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"), Some("akd/1.0 CFNetwork/811.5.4")));
        assert_eq!(both.client_info().client_info, "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>");
        assert_eq!(both.client_info().user_agent, "akd/1.0 CFNetwork/811.5.4");
        // what the server announced stays known, for the log and the diagnostics
        assert_eq!(both.announced().client_info, announced.client_info);

        let only_ua = client().with_overrides(&ClientOverrides::new(None, Some("akd/1.0 CFNetwork/811.5.4")));
        assert_eq!(only_ua.client_info().client_info, announced.client_info);
        assert_eq!(only_ua.client_info().user_agent, "akd/1.0 CFNetwork/811.5.4");
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

    /// What a server announces today (October 2026): a Mac.
    const MAC_INFO: &str = r#"{"client_info":"<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>","user_agent":"akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0"}"#;
    const MAC_CLIENT_INFO: &str = "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";
    const MAC_USER_AGENT: &str = "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0";
    /// ... what they announced until 2026-10-22: Windows.
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

    /// A server that works: announces `info` and gives headers.
    fn working(info: &'static str) -> (String, Seen) {
        serve(vec![("/v3/client_info", 200, info), ("/v3/get_headers", 200, HEADERS)])
    }

    fn run<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(future)
    }

    /// A directory holding identities, already provisioned, with each of these servers (no Apple needed).
    fn provisioned_dir(servers: &[&str]) -> (PathBuf, AnisetteState) {
        let dir = std::env::temp_dir().join(format!("omnisette-http-{}", rand::random::<u64>()));
        let state = AnisetteState {
            keychain_identifier: [9; 16],
            adi_pb: Some(vec![1, 2, 3]),
        };
        for server in servers {
            save_state(&dir, server, &state).unwrap();
        }
        (dir, state)
    }

    fn count(seen: &Seen, path: &str) -> usize {
        seen.lock().unwrap().iter().filter(|(p, _)| p == path).count()
    }

    #[test]
    fn headers_carry_the_client_info_and_user_agent_of_the_server() {
        let (url, seen) = working(MAC_INFO);
        let (dir, state) = provisioned_dir(&[&url]);
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let headers = run(provider.get_authentication_headers()).unwrap();

        assert_eq!(headers["X-Mme-Client-Info"], MAC_CLIENT_INFO);
        assert_eq!(headers["X-Apple-I-MD"], "otp");
        assert_eq!(headers["X-Apple-I-MD-M"], "machine");
        assert_eq!(headers["X-Mme-Device-Id"], state.device_id());
        assert_eq!(provider.user_agent().as_deref(), Some(MAC_USER_AGENT));
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
    fn whatever_a_server_announces_is_used_apple_decides() {
        // Windows (1.0.1's rule), Mac (today), the Android of the old servers, and something nobody has
        // seen yet: none is turned down, the data go out with the pair the server announced
        for (client_info, user_agent) in [
            ("<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>", "akd/1.0 CFNetwork/808.1.4"),
            ("<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>", "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0"),
            ("<Pixel 7> <Android;13;TQ3A.230901.001> <com.apple.AuthKit/1 (com.apple.akd/1.0)>", "akd/1.0 CFNetwork/978.0.7"),
            ("<Quantum9000> <QuantumOS;42.0> <com.apple.AuthKit/1 (com.apple.akd/9.9)>", "akd/9.9 Whatever/1.0"),
        ] {
            let info: &'static str = Box::leak(
                format!(r#"{{"client_info":"{client_info}","user_agent":"{user_agent}"}}"#).into_boxed_str(),
            );
            let (url, _) = working(info);
            let (dir, _) = provisioned_dir(&[&url]);
            let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
            let headers = run(provider.get_authentication_headers()).unwrap();
            assert_eq!(headers["X-Mme-Client-Info"], client_info);
            assert_eq!(provider.user_agent().as_deref(), Some(user_agent));
            fs::remove_dir_all(dir).unwrap();
        }
    }

    #[test]
    fn overrides_go_to_apple_instead_of_what_the_server_announces() {
        let (url, _) = working(MAC_INFO);
        let (dir, _) = provisioned_dir(&[&url]);
        let overrides = ClientOverrides::new(
            Some("<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"),
            Some("akd/1.0 CFNetwork/811.5.4"),
        );
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string()).with_overrides(overrides);
        let headers = run(provider.get_authentication_headers()).unwrap();
        assert_eq!(
            headers["X-Mme-Client-Info"],
            "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"
        );
        assert_eq!(provider.user_agent().as_deref(), Some("akd/1.0 CFNetwork/811.5.4"));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_synchronization_sends_the_sim_with_the_device() {
        let (url, seen) = working(MAC_INFO);
        let (dir, state) = provisioned_dir(&[&url]);
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
    fn a_dead_first_server_is_replaced_by_the_next() {
        let (primary, primary_seen) = serve(vec![]); // 404 everywhere, like a broken backend
        let (fallback, fallback_seen) = working(MAC_INFO);
        let (dir, _) = provisioned_dir(&[&primary, &fallback]);
        let mut provider = RemoteAnisetteProviderV3::new(primary, dir.clone(), "0".to_string())
            .with_fallbacks(vec![fallback.clone()]);
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
    fn the_servers_are_tried_in_order_until_one_works() {
        let (dead, dead_seen) = serve(vec![]);
        let (html, html_seen) = serve(vec![("/v3/client_info", 200, "<html><title>Just a moment...</title></html>")]);
        let (broken, broken_seen) = serve(vec![
            ("/v3/client_info", 200, MAC_INFO),
            ("/v3/get_headers", 500, r#"{"result":"GetHeadersError","message":"ADI error -1: busy"}"#),
        ]);
        let (good, good_seen) = working(MAC_INFO);
        let (spare, spare_seen) = working(MAC_INFO);
        let (dir, _) = provisioned_dir(&[&dead, &html, &broken, &good, &spare]);
        let mut provider = RemoteAnisetteProviderV3::with_servers(
            vec![dead.clone(), html.clone(), broken.clone(), good.clone(), spare.clone()],
            dir.clone(),
            "0".to_string(),
        );
        run(provider.get_authentication_headers()).unwrap();
        assert_eq!(provider.server_used().as_deref(), Some(good.as_str()));
        for seen in [&dead_seen, &html_seen, &broken_seen, &good_seen] {
            assert_eq!(count(seen, "/v3/client_info"), 1);
        }
        assert_eq!(count(&broken_seen, "/v3/get_headers"), 1);
        assert_eq!(count(&good_seen, "/v3/get_headers"), 1);
        // the ones behind the server that worked are not asked
        assert_eq!(count(&spare_seen, "/v3/client_info"), 0);
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn the_preferred_server_is_asked_first() {
        let (first, first_seen) = working(MAC_INFO);
        let (second, second_seen) = working(MAC_INFO);
        let (dir, _) = provisioned_dir(&[&first, &second]);
        let mut provider = RemoteAnisetteProviderV3::new(first.clone(), dir.clone(), "0".to_string())
            .with_fallbacks(vec![second.clone(), first.clone()]) // listed twice: once
            .with_preferred(Some(second.clone()));
        run(provider.get_authentication_headers()).unwrap();
        assert_eq!(provider.server_used().as_deref(), Some(second.as_str()));
        assert_eq!(count(&first_seen, "/v3/client_info"), 0);
        assert_eq!(count(&second_seen, "/v3/get_headers"), 1);
        // a preference for a server that is not in the list changes nothing
        let mut other = RemoteAnisetteProviderV3::new(first.clone(), dir.clone(), "0".to_string())
            .with_preferred(Some("http://127.0.0.1:1".to_string()));
        run(other.get_authentication_headers()).unwrap();
        assert_eq!(other.server_used().as_deref(), Some(first.as_str()));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_server_that_answers_html_is_not_usable() {
        let (url, _) = serve(vec![("/v3/client_info", 200, "<html><title>Just a moment...</title></html>")]);
        let (dir, _) = provisioned_dir(&[&url]);
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err().to_string();
        assert!(error.contains("client_info") && error.contains("Just a moment"), "{error}");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn one_server_alone_reports_its_own_error_and_several_report_them_all() {
        let (primary, _) = serve(vec![]);
        let (fallback, _) = serve(vec![("/v3/client_info", 500, "boom")]);
        let (dir, _) = provisioned_dir(&[&primary, &fallback]);

        let mut alone = RemoteAnisetteProviderV3::new(primary.clone(), dir.clone(), "0".to_string());
        let error = run(alone.get_authentication_headers()).unwrap_err();
        assert!(matches!(error, AnisetteError::ServerError(_)), "{error}");
        assert!(!matches!(error, AnisetteError::AllServersFailed(_)));

        let mut both = RemoteAnisetteProviderV3::new(primary.clone(), dir.clone(), "0".to_string())
            .with_fallbacks(vec![fallback.clone()]);
        let error = run(both.get_authentication_headers()).unwrap_err();
        let AnisetteError::AllServersFailed(text) = &error else {
            panic!("{error}")
        };
        assert!(text.contains(&primary) && text.contains(&fallback), "{text}");

        // no server at all is an error too, not a panic
        let mut none = RemoteAnisetteProviderV3::with_servers(Vec::new(), dir.clone(), "0".to_string());
        assert!(matches!(run(none.get_authentication_headers()), Err(AnisetteError::AllServersFailed(_))));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn an_error_status_does_not_hide_the_message_of_the_server() {
        // -45061 would provision again (Apple needed): another message stays a server error
        let (url, _) = serve(vec![
            ("/v3/client_info", 200, MAC_INFO),
            ("/v3/get_headers", 500, r#"{"result":"GetHeadersError","message":"ADI error -1: busy"}"#),
        ]);
        let (dir, _) = provisioned_dir(&[&url]);
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err();
        assert!(matches!(&error, AnisetteError::ServerError(m) if m.contains("busy")), "{error}");
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_windows_pair_is_still_just_a_pair() {
        // the fixture of the earlier versions keeps working: nothing in the provider looks at it
        let (url, _) = working(WINDOWS_INFO);
        let (dir, _) = provisioned_dir(&[&url]);
        let mut provider = RemoteAnisetteProviderV3::new(url, dir.clone(), "0".to_string());
        let headers = run(provider.get_authentication_headers()).unwrap();
        assert!(headers["X-Mme-Client-Info"].contains("Windows"));
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

    const MAC: &str = "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";
    const MAC_USER_AGENT: &str = "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0";
    const WINDOWS: &str =
        "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>";
    const WINDOWS_USER_AGENT: &str = "akd/1.0 CFNetwork/808.1.4";
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
        user_agent: &'static str,
        /// X-Mme-Client-Info values for which Apple's URL bag lists the provisioning URLs
        bag_lists_urls: fn(&str) -> bool,
        /// (HTTP status, JSON) of the n-th (from 0) /v3/get_headers
        get_headers: fn(usize) -> (u16, &'static str),
        /// The provisioning socket gives up right after the identifier (the server times out).
        provisioning_times_out: bool,
    }

    impl Script {
        fn mac() -> Script {
            Script {
                client_info: MAC,
                user_agent: MAC_USER_AGENT,
                bag_lists_urls: |_| true,
                get_headers: |_| (200, HEADERS),
                provisioning_times_out: false,
            }
        }

        fn windows() -> Script {
            Script {
                client_info: WINDOWS,
                user_agent: WINDOWS_USER_AGENT,
                ..Script::mac()
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
            provisioning_socket(stream, identifiers, script.provisioning_times_out);
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
                format!(
                    r#"{{"client_info":"{}","user_agent":"{}"}}"#,
                    script.client_info, script.user_agent
                ),
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
    fn provisioning_socket(stream: TcpStream, identifiers: &Mutex<Vec<String>>, times_out: bool) {
        let text = |json: &str| tungstenite::Message::Text(json.to_string().into());
        let mut socket = tungstenite::accept(stream).unwrap();
        socket.send(text(r#"{"result":"GiveIdentifier"}"#)).unwrap();
        let reply: serde_json::Value = serde_json::from_str(socket.read().unwrap().to_text().unwrap()).unwrap();
        identifiers.lock().unwrap().push(reply["identifier"].as_str().unwrap().to_string());
        if times_out {
            socket.send(text(r#"{"result":"Timeout"}"#)).unwrap();
            let _ = socket.read();
            return;
        }
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

    /// A directory holding an identity with `server` that is provisioned already.
    fn provisioned_dir(server: &str) -> (PathBuf, AnisetteState) {
        let dir = new_dir();
        let state = AnisetteState {
            keychain_identifier: [9; 16],
            adi_pb: Some(vec![1, 2, 3]),
        };
        save_state(&dir, server, &state).unwrap();
        (dir, state)
    }

    fn json(text: &str) -> serde_json::Value {
        serde_json::from_str(text).unwrap()
    }

    #[test]
    fn provisioning_requests_carry_the_headers_of_a_client() {
        let mock = serve(Script::mac());
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
            assert_eq!(h["x-mme-client-info"], MAC, "{path}");
            assert_eq!(h["user-agent"], MAC_USER_AGENT, "{path}");
            assert_eq!(h["content-type"], "text/x-xml-plist", "{path}");
            assert_eq!(h["accept"], "text/x-xml-plist", "{path}");
            assert_eq!(h["x-xcode-version"], XCODE_VERSION, "{path}");
            assert_eq!(h["x-apple-app-info"], "com.apple.gs.xcode.auth", "{path}");
            assert_eq!(h["connection"], "close", "{path}");
            assert_eq!(h["x-apple-i-md-lu"].len(), 64, "{path}");
            assert_eq!(h["x-mme-device-id"].len(), 36, "{path}");
        }

        // the identity that went through the provisioning is the one that was saved, with this server
        let saved = load_state(&dir, &mock.url);
        assert!(saved.is_provisioned());
        assert_eq!(*mock.identifiers.lock().unwrap(), vec![base64_encode(&saved.keychain_identifier)]);
        assert_eq!(
            mock.requests("/apple/start")[0].headers["x-mme-device-id"],
            saved.device_id()
        );
        assert!(state_path(&dir, &mock.url).exists());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn the_overrides_go_to_apple_with_the_provisioning_too() {
        let mock = serve(Script::mac());
        let _apple = LookupOverride::to(&mock);
        let dir = new_dir();
        let overrides = ClientOverrides::new(
            Some("<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"),
            Some("akd/1.0 CFNetwork/811.5.4"),
        );
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string())
            .with_overrides(overrides);
        let headers = run(provider.get_authentication_headers()).unwrap();
        assert_eq!(
            headers["X-Mme-Client-Info"],
            "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"
        );
        for path in ["/apple/lookup", "/apple/start", "/apple/finish"] {
            let h = &mock.requests(path)[0].headers;
            assert_eq!(h["x-mme-client-info"], "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>", "{path}");
            assert_eq!(h["user-agent"], "akd/1.0 CFNetwork/811.5.4", "{path}");
        }
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
        // the Mac client info is for the bag only: the provisioning itself keeps the server's pair
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
            ..Script::mac()
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
            ..Script::mac()
        });
        let _apple = LookupOverride::to(&mock);
        let (dir, old) = provisioned_dir(&mock.url);
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

        let saved = load_state(&dir, &mock.url);
        assert_eq!(base64_encode(&saved.keychain_identifier), second["identifier"].as_str().unwrap());
        assert_eq!(saved.adi_pb, Some(b"adi-provisioned".to_vec()));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_keeps_saying_not_provisioned_is_asked_once_more_and_no_more() {
        let mock = serve(Script {
            get_headers: |_| (500, NOT_PROVISIONED),
            ..Script::mac()
        });
        let _apple = LookupOverride::to(&mock);
        let (dir, _) = provisioned_dir(&mock.url);
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        let error = run(provider.get_authentication_headers()).unwrap_err();
        assert!(matches!(error, AnisetteError::AnisetteNotProvisioned), "{error}");
        assert_eq!(mock.requests("/v3/get_headers").len(), 2);
        assert_eq!(mock.identifiers.lock().unwrap().len(), 1, "one provisioning, no loop");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn minus_45061_that_stays_after_one_new_identity_hands_over_to_the_next_server() {
        let stubborn = serve(Script {
            get_headers: |_| (500, NOT_PROVISIONED),
            ..Script::mac()
        });
        let good = serve(Script::mac());
        let _apple = LookupOverride::to(&good);
        let (dir, old) = provisioned_dir(&stubborn.url);
        let mut provider = RemoteAnisetteProviderV3::new(stubborn.url.clone(), dir.clone(), "0".to_string())
            .with_fallbacks(vec![good.url.clone()]);
        let headers = run(provider.get_authentication_headers()).unwrap();
        assert_eq!(headers["X-Apple-I-MD"], "otp");
        assert_eq!(provider.server_used().as_deref(), Some(good.url.as_str()));

        // the first server: its old identity, one new identity, and that is all
        assert_eq!(stubborn.requests("/v3/get_headers").len(), 2);
        assert_eq!(stubborn.identifiers.lock().unwrap().len(), 1);
        // the next one made an identity of its own, provisioned through its own socket
        assert_eq!(good.identifiers.lock().unwrap().len(), 1);
        assert!(provider.identity_is_new());
        let on_stubborn = load_state(&dir, &stubborn.url);
        let on_good = load_state(&dir, &good.url);
        assert_ne!(on_good.keychain_identifier, on_stubborn.keychain_identifier);
        assert_ne!(on_good.keychain_identifier, old.keychain_identifier);
        assert_eq!(*good.identifiers.lock().unwrap(), vec![base64_encode(&on_good.keychain_identifier)]);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_server_that_cannot_provision_hands_over_and_the_next_one_gets_its_own_identity() {
        let timing_out = serve(Script {
            provisioning_times_out: true,
            ..Script::mac()
        });
        let good = serve(Script::mac());
        let _apple = LookupOverride::to(&good);
        let dir = new_dir();
        let mut provider = RemoteAnisetteProviderV3::new(timing_out.url.clone(), dir.clone(), "0".to_string())
            .with_fallbacks(vec![good.url.clone()]);
        run(provider.get_authentication_headers()).unwrap();
        assert_eq!(provider.server_used().as_deref(), Some(good.url.as_str()));
        assert!(provider.identity_is_new());
        // the failed provisioning saved nothing for the first server; the second has its identity
        assert!(!state_path(&dir, &timing_out.url).exists());
        let saved = load_state(&dir, &good.url);
        assert!(saved.is_provisioned());
        assert_eq!(*good.identifiers.lock().unwrap(), vec![base64_encode(&saved.keychain_identifier)]);
        assert_ne!(
            timing_out.identifiers.lock().unwrap()[0],
            good.identifiers.lock().unwrap()[0],
            "two servers never share an identifier"
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_new_identity_of_a_server_that_failed_afterwards_is_not_reported_for_the_next_one() {
        // the first server provisions an identity, then fails to give headers; the second one has an
        // identity already: the data we return belong to an identity Apple may know, not a new one
        let failing = serve(Script {
            get_headers: |_| (500, r#"{"result":"GetHeadersError","message":"ADI error -1: busy"}"#),
            ..Script::mac()
        });
        let good = serve(Script::mac());
        let _apple = LookupOverride::to(&failing);
        let (dir, _) = provisioned_dir(&good.url);
        let mut provider = RemoteAnisetteProviderV3::new(failing.url.clone(), dir.clone(), "0".to_string())
            .with_fallbacks(vec![good.url.clone()]);
        run(provider.get_authentication_headers()).unwrap();
        assert_eq!(provider.server_used().as_deref(), Some(good.url.as_str()));
        assert_eq!(failing.identifiers.lock().unwrap().len(), 1, "the first server did provision one");
        assert!(!provider.identity_is_new());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_mac_server_is_used_and_nothing_is_held_against_it() {
        // 1.0.1 refused such a server before sending anything to Apple; Apple decides now
        let mock = serve(Script::mac());
        let _apple = LookupOverride::to(&mock);
        let (dir, _) = provisioned_dir(&mock.url);
        let mut provider = RemoteAnisetteProviderV3::new(mock.url.clone(), dir.clone(), "0".to_string());
        let headers = run(provider.get_authentication_headers()).unwrap();
        assert_eq!(headers["X-Mme-Client-Info"], MAC);
        assert_eq!(provider.user_agent().as_deref(), Some(MAC_USER_AGENT));
        assert_eq!(mock.requests("/v3/get_headers").len(), 1);
        let _ = fs::remove_dir_all(dir);
    }
}
