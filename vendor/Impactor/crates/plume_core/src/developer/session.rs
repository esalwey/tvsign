use omnisette::AnisetteConfiguration;
use reqwest::header::HeaderName;
use std::sync::Arc;
use tokio::sync::Mutex;

use plist::{Dictionary, Value};
use reqwest::Client;
use reqwest::header::HeaderMap;
use reqwest::header::HeaderValue;
use uuid::Uuid;

use crate::Error;

use crate::auth::Account;
use crate::auth::account::{MAX_SERVER_SWITCHES, snippet};
use crate::auth::anisette_data::AnisetteData;
use crate::developer::qh::QHResponseMeta;
use crate::developer::v1::V1ErrorResponse;

pub struct DeveloperSession {
    anisette: Arc<Mutex<AnisetteData>>,
    client: Client,
    adsid: String,          // from grandslam's SPD "adsid"
    xcode_gs_token: String, // requested from spd initially // com.apple.gs.xcode.auth
}

impl DeveloperSession {
    pub async fn using_account(account: Account) -> Result<Self, Error> {
        let adsid = account
            .spd
            .as_ref()
            .and_then(|spd| spd.get("adsid"))
            .and_then(Value::as_string)
            .ok_or(Error::Parse)?
            .to_string();
        let xcode_gs_token = account
            .get_app_token("com.apple.gs.xcode.auth")
            .await?
            .auth_token;

        Ok(DeveloperSession {
            anisette: account.anisette.clone(),
            client: account.client.clone(),
            adsid,
            xcode_gs_token,
        })
    }

    pub async fn new(
        adsid: String,
        xcode_gs_token: String,
        config: AnisetteConfiguration,
    ) -> Result<Self, Error> {
        let anisette = AnisetteData::new(config).await?;
        Self::new_synchronizing(adsid, xcode_gs_token, anisette).await
    }

    /// A session with anisette data that may belong to an identity Apple has never seen
    /// (`fresh_identity`). A renewal never signs in, so nothing has let Apple see a new identity
    /// synchronize, and the portal may refuse the data of an identity it does not know. When the
    /// first request fails for such an identity, ask GrandSlam once (a throwaway Apple ID, no
    /// account is touched) and try again with synchronized data. Best effort: the first error is the
    /// one reported when the synchronization does not happen.
    ///
    /// A portal that answers HTTP 503 (its page, not a plist) is refusing the data of the anisette
    /// server in use, or is overloaded: the data of the next server are tried, once per server. The
    /// server whose data the portal accepts is remembered for the next time.
    pub(crate) async fn new_synchronizing(
        adsid: String,
        xcode_gs_token: String,
        anisette: AnisetteData,
    ) -> Result<Self, Error> {
        let mut anisette = anisette;
        let mut switches = 0;
        loop {
            let tried = Self::new_synchronizing_with(adsid.clone(), xcode_gs_token.clone(), anisette.clone()).await;
            match tried {
                Err(first @ Error::DeveloperHttp { status: 503, .. }) if switches < MAX_SERVER_SWITCHES => {
                    switches += 1;
                    match anisette.from_other_server().await {
                        Ok(Some(next)) => {
                            log::warn!(
                                "the developer portal refuses the data of {} ({first}): using {} instead",
                                anisette.server.as_deref().unwrap_or("the anisette server"),
                                next.server.as_deref().unwrap_or("another anisette server")
                            );
                            anisette = next;
                        }
                        Ok(None) => return Err(first),
                        Err(e) => {
                            log::warn!("the other anisette servers cannot be used either: {e}");
                            return Err(first);
                        }
                    }
                }
                Ok(session) => {
                    // the portal accepted these data: this server is the one to try first next time
                    session.anisette.lock().await.confirm_server();
                    return Ok(session);
                }
                other => return other,
            }
        }
    }

    /// [`Self::new_synchronizing`] with the data of one server.
    async fn new_synchronizing_with(
        adsid: String,
        xcode_gs_token: String,
        anisette: AnisetteData,
    ) -> Result<Self, Error> {
        let fresh_identity = anisette.fresh_identity;
        let anisette = Arc::new(Mutex::new(anisette));
        match Self::new_with_anisette(adsid.clone(), xcode_gs_token.clone(), anisette.clone()).await {
            Err(first) if fresh_identity && could_be_an_unknown_identity(&first) => {
                log::info!(
                    "the developer portal refused a new anisette identity ({first}): synchronizing it with Apple"
                );
                let current = anisette.lock().await.clone();
                match current.synchronize_with_apple().await {
                    Ok(Some(synced)) => {
                        *anisette.lock().await = synced;
                        Self::new_with_anisette(adsid, xcode_gs_token, anisette).await
                    }
                    Ok(None) => Err(first),
                    Err(e) => {
                        log::warn!("anisette synchronization with Apple skipped: {e}");
                        Err(first)
                    }
                }
            }
            other => other,
        }
    }

    pub async fn new_with_anisette(
        adsid: String,
        xcode_gs_token: String,
        anisette: Arc<Mutex<AnisetteData>>,
    ) -> Result<Self, Error> {
        let client = crate::client()?;

        let s = Self {
            anisette,
            client,
            adsid,
            xcode_gs_token,
        };

        // we test the session by listing teams
        // if this fails, the session is invalid (obviously)
        s.qh_list_teams().await?;

        Ok(s)
    }

    pub fn adsid(&self) -> &String {
        &self.adsid
    }

    pub fn xcode_gs_token(&self) -> &String {
        &self.xcode_gs_token
    }
}

impl DeveloperSession {
    pub async fn qh_send_request(
        &self,
        url: &str,
        body: Option<Dictionary>,
    ) -> Result<Dictionary, Error> {
        let mut headers = HeaderMap::new();
        headers.insert("Content-Type", HeaderValue::from_static("text/x-xml-plist"));
        headers.insert("Accept", HeaderValue::from_static("text/x-xml-plist"));
        self.insert_identity_headers(&mut headers).await;
        self.insert_anisette_headers(&mut headers).await?;

        let mut body = body.unwrap_or_default();
        body.insert(
            "requestId".into(),
            Value::String(Uuid::new_v4().to_string().to_uppercase()),
        );

        let request_builder = self.client.post(url).headers(headers);

        let mut buffer = Vec::new();
        plist::to_writer_xml(&mut buffer, &body)?;

        log::debug!("QH Request to {}: {:?}", url, body);

        let response = request_builder.body(buffer).send().await?;
        let status = response.status().as_u16();
        let response_bytes = response.bytes().await?;
        let response_dict: Dictionary = match plist::from_bytes(&response_bytes) {
            Ok(dict) => dict,
            // the HTML page of an error (503: Apple refuses the anisette data, or is overloaded)
            Err(_) if !is_plist(&response_bytes) => {
                return Err(Error::DeveloperHttp {
                    status,
                    body: snippet(&String::from_utf8_lossy(&response_bytes)),
                });
            }
            Err(e) => return Err(e.into()),
        };

        log::debug!("QH Response from {}: {:?}", url, response_dict);

        let response_meta: QHResponseMeta =
            plist::from_value(&Value::Dictionary(response_dict.clone()))?;

        if response_meta.result_code.as_signed().unwrap_or(0) != 0 {
            return Err(response_meta.to_error(url.to_string()));
        }

        Ok(response_dict)
    }

    pub async fn v1_send_request(
        &self,
        url: &str,
        body: Option<serde_json::Value>,
        request_type: Option<RequestType>,
    ) -> Result<serde_json::Value, Error> {
        let mut headers = HeaderMap::new();
        headers.insert(
            "Content-Type",
            HeaderValue::from_static("application/vnd.api+json"),
        );
        headers.insert(
            "Accept",
            HeaderValue::from_static("application/json, text/plain, */*"),
        );
        headers.insert(
            "X-Requested-With",
            HeaderValue::from_static("XMLHttpRequest"),
        );
        self.insert_identity_headers(&mut headers).await;
        if let Some(RequestType::Get) = request_type {
            headers.insert("X-HTTP-Method-Override", HeaderValue::from_static("GET"));
        }
        self.insert_anisette_headers(&mut headers).await?;

        let mut request_builder = match request_type {
            Some(RequestType::Patch) => self.client.patch(url).headers(headers.clone()),
            Some(RequestType::Post) | _ if body.is_some() => {
                self.client.post(url).headers(headers.clone())
            }
            _ => self.client.get(url).headers(headers.clone()),
        };

        log::debug!("V1 Request to {}: {:?}", url, &body);

        if let Some(body) = body {
            request_builder = request_builder.json(&body);
        }

        let response = request_builder.send().await?;
        let status = response.status().as_u16();
        let response_text = response.text().await?;

        log::debug!("V1 Response from {}: {}", url, response_text);

        let response_json: serde_json::Value = match serde_json::from_str(&response_text) {
            Ok(json) => json,
            // the HTML page of an error, or an HTTP error with no JSON in it
            Err(_) if status >= 400 || response_text.trim_start().starts_with('<') => {
                return Err(Error::DeveloperHttp {
                    status,
                    body: snippet(&response_text),
                });
            }
            Err(e) => return Err(e.into()),
        };

        if let Ok(errors) = serde_json::from_value::<V1ErrorResponse>(response_json.clone()) {
            return Err(errors.errors[0].to_error(url.to_string()));
        }

        Ok(response_json)
    }

    // TODO: this can be deduplicated as well, for reuse in `fn build_2fa_headers`
    async fn insert_identity_headers(&self, headers: &mut HeaderMap) {
        headers.insert("Accept-Language", HeaderValue::from_static("en-us"));
        headers.insert(
            "X-Apple-I-Identity-Id",
            HeaderValue::from_str(&self.adsid).unwrap(),
        );
        headers.insert(
            "X-Apple-GS-Token",
            HeaderValue::from_str(&self.xcode_gs_token).unwrap(),
        );
    }

    /// The anisette data and the device they belong to: the client info and the user agent that
    /// the anisette server reported (the user agent used to be a fixed `Xcode`).
    async fn insert_anisette_headers(&self, headers: &mut HeaderMap) -> Result<(), Error> {
        let valid_anisette = self.get_anisette().await?;
        for (k, v) in valid_anisette.generate_headers(false, true, true) {
            if let (Ok(name), Ok(value)) = (
                HeaderName::from_bytes(k.as_bytes()),
                HeaderValue::from_str(&v),
            ) {
                headers.insert(name, value);
            }
        }
        if let Ok(value) = HeaderValue::from_str(&valid_anisette.user_agent) {
            headers.insert("User-Agent", value);
        }
        if let Ok(locale) = valid_anisette.get_header("x-apple-locale") {
            if let Ok(value) = HeaderValue::from_str(&locale) {
                headers.insert("X-Apple-Locale", value);
            }
        }
        Ok(())
    }

    // TODO: deduplicate?
    pub async fn get_anisette(&self) -> Result<AnisetteData, Error> {
        let mut locked = self.anisette.lock().await;
        if locked.needs_refresh() {
            locked.refresh_in_place().await?;
        }
        Ok(locked.clone())
    }
}

/// A body that is a plist (XML or binary), not the HTML page of an error.
fn is_plist(body: &[u8]) -> bool {
    let body = body.trim_ascii_start();
    body.starts_with(b"<?xml") || body.starts_with(b"<plist") || body.starts_with(b"bplist")
}

/// Whether the failure of the first request of a session could be the portal not accepting the
/// data of an identity it has never seen (anything but a network failure).
fn could_be_an_unknown_identity(error: &Error) -> bool {
    !matches!(error, Error::Reqwest(_) | Error::Io(_) | Error::Anisette(_))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestType {
    Get,
    Post,
    Patch,
}
