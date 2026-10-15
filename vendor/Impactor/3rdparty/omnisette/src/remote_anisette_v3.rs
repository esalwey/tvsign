// Implementing the SideStore Anisette v3 protocol
//
// tvsign: the default server serves Windows anisette (Apple refuses Android anisette since
// 2026-10-12) with a fallback server, the client info AND the user agent of the server are kept,
// the identity lives in its own state file, a server answer of -45061 provisions again, Apple's
// synchronization request (hsc 434) is relayed to the server. See vendor/Impactor/PATCHES.md.
// The protocol handling follows isideload (https://github.com/nab138/isideload, MIT).

use std::{
    collections::HashMap,
    fs,
    io::Cursor,
    path::{Path, PathBuf},
    sync::Mutex,
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

use crate::{AnisetteError, anisette_headers_provider::AnisetteHeadersProvider};

/// File holding the anisette identity (device identifier + provisioning). The name changed with the
/// Windows anisette: the Android identities kept in `state.plist` are useless to Apple now.
pub const STATE_FILE: &str = "anisette-state-windows.plist";
/// Where the Android-era identity was kept; removed once the new one is saved.
pub const LEGACY_STATE_FILE: &str = "state.plist";

const PROVISIONING_TIMEOUT: Duration = Duration::from_secs(60);

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

/// Forget the provisioning but keep the device identifier (see [`crate::invalidate_provisioning`]).
pub fn invalidate_provisioning(dir: &Path) -> Result<(), AnisetteError> {
    if !dir.join(STATE_FILE).exists() {
        return Ok(());
    }
    let mut state = load_state(dir);
    state.adi_pb = None;
    save_state(dir, &state)
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
        Ok(AnisetteClient { client_info, url })
    }

    pub fn client_info(&self) -> &AnisetteClientInfo {
        &self.client_info
    }

    /// Apple only accepts Windows anisette now, whatever a server announces about itself is
    /// judged here (a Mac client info over Android data is refused as well).
    pub fn serves_windows(&self) -> bool {
        self.client_info.client_info.contains("Windows")
    }

    fn build_apple_request(
        &self,
        state: &AnisetteState,
        builder: RequestBuilder,
    ) -> RequestBuilder {
        let dt: DateTime<Utc> = Utc::now().round_subsecs(0);

        builder
            .header("X-Mme-Client-Info", &self.client_info.client_info)
            .header("User-Agent", &self.client_info.user_agent)
            .header("Content-Type", "text/x-xml-plist")
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

    pub async fn provision(&self, state: &mut AnisetteState) -> Result<(), AnisetteError> {
        debug!("Provisioning Anisette");
        let http_client = make_reqwest()?;
        let resp = self
            .build_apple_request(
                &state,
                http_client.get("https://gsa.apple.com/grandslam/GsService2/lookup"),
            )
            .send()
            .await?;
        let bag = apple_dict(resp).await?;
        let urls = bag
            .get("urls")
            .and_then(Value::as_dictionary)
            .ok_or_else(|| AnisetteError::ProvisioningFailed("Apple's URL bag has no urls".to_string()))?;
        let start_provisioning_url = plist_string(urls, "midStartProvisioning")?;
        let end_provisioning_url = plist_string(urls, "midFinishProvisioning")?;
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
                            .build_apple_request(state, http_client.post(start_provisioning_url))
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
                            .build_apple_request(state, http_client.post(end_provisioning_url))
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

/// Server that served the last request of this process: tried first next time, so a dead default
/// server costs its connection timeout once, not at every refresh of the headers.
static LAST_WORKING_SERVER: Mutex<Option<String>> = Mutex::new(None);

fn remember_server(url: &str) {
    if let Ok(mut last) = LAST_WORKING_SERVER.lock() {
        *last = Some(url.to_string());
    }
}

fn last_server() -> Option<String> {
    LAST_WORKING_SERVER.lock().ok().and_then(|last| last.clone())
}

/// Servers to try, in order: the primary then the fallback, the one that worked last first.
fn server_order(primary: &str, fallback: Option<&str>, last: Option<&str>) -> Vec<String> {
    let mut order = vec![primary.to_string()];
    if let Some(fallback) = fallback.filter(|f| *f != primary) {
        order.push(fallback.to_string());
    }
    if let Some(last) = last
        && let Some(position) = order.iter().position(|server| server == last)
        && position > 0
    {
        let server = order.remove(position);
        order.insert(0, server);
    }
    order
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
    client_info: Option<AnisetteClientInfo>,
    pub state: Option<AnisetteState>,
    configuration_path: PathBuf,
    serial: String,
    new_identity: bool,
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
            client_info: None,
            state: None,
            configuration_path,
            serial,
            new_identity: false,
        }
    }

    /// Server to use when the main one fails.
    pub fn with_fallback(mut self, fallback_url: Option<String>) -> RemoteAnisetteProviderV3 {
        self.fallback_url = fallback_url;
        self
    }

    /// Headers from one server: provisions the identity when needed, provisions it again when the
    /// server says it is not valid (-45061), synchronizes first when a `sim` is given.
    async fn headers_from(
        &mut self,
        server: &str,
        sim: Option<&str>,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        let client = AnisetteClient::new(server.to_string()).await?;
        if !client.serves_windows() {
            warn!(
                "{server} announces `{}`: only Windows anisette is accepted by Apple, expect an error (-80045, HTTP 503)",
                client.client_info().client_info
            );
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
                warn!("{server} answered -45061 (identity not provisioned): provisioning again");
                state.adi_pb = None;
                client.provision(state).await?;
                self.new_identity = true;
                save_state(&self.configuration_path, state)?;
                client.get_headers(state, sim).await?
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
            last_server().as_deref(),
        );
        let mut failures: Vec<String> = Vec::new();
        let mut first_error: Option<AnisetteError> = None;
        for server in order {
            match self.headers_from(&server, sim).await {
                Ok(headers) => {
                    remember_server(&server);
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
    fn servers_are_tried_in_order_with_the_last_good_one_first() {
        let p = "https://a.example";
        let f = "https://b.example";
        assert_eq!(server_order(p, Some(f), None), vec![p, f]);
        assert_eq!(server_order(p, Some(f), Some(p)), vec![p, f]);
        assert_eq!(server_order(p, Some(f), Some(f)), vec![f, p]);
        assert_eq!(server_order(p, Some(f), Some("https://c.example")), vec![p, f]);
        assert_eq!(server_order(p, None, Some(f)), vec![p]);
        assert_eq!(server_order(p, Some(p), None), vec![p]);
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

        invalidate_provisioning(&dir).unwrap();
        let loaded = load_state(&dir);
        assert_eq!(loaded.keychain_identifier, state.keychain_identifier);
        assert!(!loaded.is_provisioned());
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
        // the server that worked is remembered: the next provider of this process starts with it
        assert_eq!(last_server().as_deref(), Some(fallback.as_str()));
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
