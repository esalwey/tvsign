use std::collections::HashMap;
use std::time::{Duration, Instant, SystemTime};

use omnisette::{AnisetteConfiguration, AnisetteError, AnisetteHeaders};
use reqwest::header::{HeaderMap, HeaderValue};

use crate::Error;

/// `X-Xcode-Version` of the GrandSlam, 2FA and developer requests (the provisioning requests of the
/// anisette identity send the same one).
pub const XCODE_VERSION: &str = omnisette::remote_anisette_v3::XCODE_VERSION;
/// User agent used when the anisette provider does not report one with its client info.
pub const DEFAULT_USER_AGENT: &str = "akd/1.0 CFNetwork/808.1.4";
/// GrandSlam answers HTTP 503 to a client info that names Xcode (since September 2026).
const BLOCKED_CLIENT_ID: &str = "com.apple.dt.Xcode";
const REPLACEMENT_CLIENT_ID: &str = "com.apple.akd/1.0";

#[derive(Debug, Clone)]
pub struct AnisetteData {
    pub base_headers: HashMap<String, String>,
    pub generated_at: SystemTime,
    pub config: AnisetteConfiguration,
    /// User-Agent reported by the anisette server with its client info. Apple compares the two:
    /// they are sent together, unchanged.
    pub user_agent: String,
    /// The anisette identity was provisioned while these headers were produced: Apple has never
    /// seen it, its first GrandSlam request will be answered with hsc 434 (synchronize).
    pub fresh_identity: bool,
    /// Last refresh given up after its tries: during `REFRESH_BACKOFF` the previous data are kept
    /// instead of trying (and waiting) at every request to Apple.
    pub(crate) refresh_failed_at: Option<Instant>,
}

/// The client info as the server reported it. Only a client identifier that GrandSlam blocks
/// (Xcode) is replaced; a Windows client info (`com.apple.iTunes/...`) is sent as it is, any change
/// to it is refused with HTTP 503.
pub fn sanitize_client_info(info: &str) -> String {
    let Some(start) = info.find(BLOCKED_CLIENT_ID) else {
        return info.to_string();
    };
    let rest = &info[start..];
    let end = rest.find([')', '>']).unwrap_or(rest.len());
    format!("{}{}{}", &info[..start], REPLACEMENT_CLIENT_ID, &rest[end..])
}

fn header_value(value: &str) -> Result<HeaderValue, Error> {
    HeaderValue::from_str(value).map_err(|_| {
        Error::Anisette(AnisetteError::InvalidArgument(format!(
            "invalid header value {value:?}"
        )))
    })
}

/// Pause apres un renouvellement abandonne.
const REFRESH_BACKOFF: Duration = Duration::from_secs(30);

impl AnisetteData {
    pub async fn new(config: AnisetteConfiguration) -> Result<Self, Error> {
        let mut b = AnisetteHeaders::get_anisette_headers_provider(config.clone())?;
        let base_headers = b.provider.get_authentication_headers().await?;

        Ok(AnisetteData {
            base_headers,
            generated_at: SystemTime::now(),
            user_agent: b
                .provider
                .user_agent()
                .unwrap_or_else(|| DEFAULT_USER_AGENT.to_string()),
            fresh_identity: b.provider.identity_is_new(),
            // the server that answered is the first one tried by the next refresh
            config: config.set_preferred_url_v3(b.provider.server_used()),
            refresh_failed_at: None,
        })
    }

    pub fn needs_refresh(&self) -> bool {
        self.age_secs() > 60
    }

    pub fn is_valid(&self) -> bool {
        self.age_secs() < 90
    }

    fn age_secs(&self) -> u64 {
        // a clock that went backwards counts as brand new
        self.generated_at.elapsed().unwrap_or_default().as_secs()
    }

    pub async fn refresh(&self) -> Result<Self, crate::Error> {
        Self::new(self.config.clone()).await
    }

    /// New headers after GrandSlam asked for a synchronization (hsc 434, `sim` being its
    /// `X-Apple-I-MD-DATA`): the anisette server synchronizes the identity with Apple first. A server
    /// that has lost the identity (-45061) makes a new one instead: the data are then those of an
    /// identity Apple has never seen (`fresh_identity`), which is asked to synchronize in its turn.
    pub async fn resync(&self, sim: &str) -> Result<Self, Error> {
        let mut b = AnisetteHeaders::get_anisette_headers_provider(self.config.clone())?;
        let headers = b.provider.resync_anisette_headers(sim).await?;
        let base_headers = b.provider.normalize_headers(headers);

        Ok(AnisetteData {
            base_headers,
            generated_at: SystemTime::now(),
            user_agent: b
                .provider
                .user_agent()
                .unwrap_or_else(|| self.user_agent.clone()),
            fresh_identity: b.provider.identity_is_new(),
            config: self.config.clone().set_preferred_url_v3(b.provider.server_used()),
            refresh_failed_at: None,
        })
    }

    /// Fetches the data again in place: three tries with a pause (a failover to the other server is
    /// part of each try), then, when the previous data are still valid, they are kept instead of
    /// failing in the middle of a sign or a 2FA wait. After an abandoned refresh no new attempt is made
    /// for `REFRESH_BACKOFF` as long as the previous data are valid, so an outage does not add its
    /// retries to every request to Apple. Data that are not valid any more are an error: Apple would
    /// only refuse them. A server that cannot be used at all (not Windows) is not retried.
    pub async fn refresh_in_place(&mut self) -> Result<(), Error> {
        let backoff = self.in_backoff(Instant::now());
        if backoff && self.is_valid() {
            return Ok(());
        }
        let tries = if backoff { 1 } else { 3 };
        let mut delay = Duration::from_secs(1);
        let mut last = None;
        for attempt in 1..=tries {
            match self.refresh().await {
                Ok(fresh) => {
                    *self = fresh;
                    return Ok(());
                }
                Err(e @ Error::Anisette(AnisetteError::UnsupportedServer(_))) => return Err(e),
                Err(e) => {
                    log::warn!("anisette refresh failed (attempt {attempt}/{tries}): {e}");
                    last = Some(e);
                    if attempt < tries {
                        tokio::time::sleep(delay).await;
                        delay *= 2;
                    }
                }
            }
        }
        self.refresh_failed_at = Some(Instant::now());
        match last {
            Some(e) if !self.is_valid() => Err(e),
            _ => {
                log::warn!("anisette refresh abandoned, keeping the previous data");
                Ok(())
            }
        }
    }

    /// A refresh has just been abandoned: no new try before `REFRESH_BACKOFF`.
    fn in_backoff(&self, now: Instant) -> bool {
        self.refresh_failed_at
            .is_some_and(|failed| now.saturating_duration_since(failed) < REFRESH_BACKOFF)
    }

    pub fn generate_headers(
        &self,
        cpd: bool,
        client_info: bool,
        app_info: bool,
    ) -> HashMap<String, String> {
        if !self.is_valid() {
            log::warn!("anisette data older than 90 s: Apple will probably refuse them");
        }

        let mut headers = self.base_headers.clone();
        let old_client_info = headers.remove("X-Mme-Client-Info");

        if client_info && let Some(value) = old_client_info {
            headers.insert("X-Mme-Client-Info".to_owned(), sanitize_client_info(&value));
        }

        if app_info {
            headers.insert(
                "X-Apple-App-Info".to_owned(),
                "com.apple.gs.xcode.auth".to_owned(),
            );
            headers.insert("X-Xcode-Version".to_owned(), XCODE_VERSION.to_owned());
        }

        if cpd {
            headers.insert("bootstrap".to_owned(), "true".to_owned());
            headers.insert("icscrec".to_owned(), "true".to_owned());
            headers.insert("loc".to_owned(), "en_GB".to_owned());
            headers.insert("pbe".to_owned(), "false".to_owned());
            headers.insert("prkgen".to_owned(), "true".to_owned());
            headers.insert("svct".to_owned(), "iCloud".to_owned());
        }

        headers
    }

    pub fn to_plist(&self, cpd: bool, client_info: bool, app_info: bool) -> plist::Dictionary {
        let mut plist = plist::Dictionary::new();
        for (key, value) in self.generate_headers(cpd, client_info, app_info).iter() {
            plist.insert(key.to_owned(), plist::Value::String(value.to_owned()));
        }

        plist
    }

    /// Value of a header (name compared without case), unchanged.
    pub fn get_header(&self, header: &str) -> Result<String, Error> {
        let wanted = header.to_lowercase();
        self.generate_headers(true, true, true)
            .into_iter()
            .find(|(name, _)| name.to_lowercase() == wanted)
            .map(|(_, value)| value)
            .ok_or(Error::DeveloperSessionRequestFailed)
    }

    /// `X-Mme-Client-Info` to send, exactly what the anisette server reported.
    pub fn client_info(&self) -> Result<String, Error> {
        self.get_header("x-mme-client-info")
    }

    /// Headers that make a request look like the device the anisette data belongs to:
    /// client info + user agent from the anisette server (Apple refuses any other pair with an
    /// HTTP 503 page), Xcode version, app info. `sms` selects JSON instead of plist bodies.
    pub fn grandslam_headers(&self, sms: bool) -> Result<HeaderMap, Error> {
        let mut headers = HeaderMap::new();
        let (content_type, accept) = if sms {
            ("application/json", "application/json")
        } else {
            ("text/x-xml-plist", "text/x-xml-plist")
        };
        headers.insert("Content-Type", HeaderValue::from_static(content_type));
        headers.insert("Accept", HeaderValue::from_static(accept));
        headers.insert("User-Agent", header_value(&self.user_agent)?);
        headers.insert("X-Mme-Client-Info", header_value(&self.client_info()?)?);
        headers.insert("X-Xcode-Version", HeaderValue::from_static(XCODE_VERSION));
        headers.insert(
            "X-Apple-App-Info",
            HeaderValue::from_static("com.apple.gs.xcode.auth"),
        );
        headers.insert("Connection", HeaderValue::from_static("close"));
        Ok(headers)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WINDOWS: &str =
        "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>";

    fn data(client_info: &str) -> AnisetteData {
        let mut base_headers = HashMap::new();
        base_headers.insert("X-Mme-Client-Info".to_string(), client_info.to_string());
        base_headers.insert("X-Apple-I-MD".to_string(), "Mixed/Case+OTP==".to_string());
        base_headers.insert("X-Apple-Locale".to_string(), "en_US".to_string());
        AnisetteData {
            base_headers,
            generated_at: SystemTime::now(),
            config: AnisetteConfiguration::default(),
            user_agent: "akd/1.0 CFNetwork/808.1.4".to_string(),
            fresh_identity: false,
            refresh_failed_at: None,
        }
    }

    #[test]
    fn windows_client_info_is_sent_unchanged() {
        assert_eq!(sanitize_client_info(WINDOWS), WINDOWS);
        assert_eq!(data(WINDOWS).client_info().unwrap(), WINDOWS);
    }

    #[test]
    fn only_the_blocked_xcode_identifier_is_replaced() {
        assert_eq!(
            sanitize_client_info(
                "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.dt.Xcode/3594.4.19)>"
            ),
            "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"
        );
        let akd = "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";
        assert_eq!(sanitize_client_info(akd), akd);
    }

    #[test]
    fn header_values_keep_their_case() {
        let d = data(WINDOWS);
        assert_eq!(d.get_header("x-apple-i-md").unwrap(), "Mixed/Case+OTP==");
        assert_eq!(d.get_header("X-APPLE-LOCALE").unwrap(), "en_US");
        assert!(d.get_header("x-nothing").is_err());
    }

    #[test]
    fn grandslam_headers_pair_client_info_and_user_agent_of_the_server() {
        let h = data(WINDOWS).grandslam_headers(false).unwrap();
        assert_eq!(h["X-Mme-Client-Info"], WINDOWS);
        assert_eq!(h["User-Agent"], "akd/1.0 CFNetwork/808.1.4");
        assert_eq!(h["Content-Type"], "text/x-xml-plist");
        assert_eq!(h["Accept"], "text/x-xml-plist");
        assert_eq!(h["X-Xcode-Version"], XCODE_VERSION);
        assert_eq!(h["X-Apple-App-Info"], "com.apple.gs.xcode.auth");
        assert_eq!(h["Connection"], "close");
        // the old fixed macOS user agent is gone
        assert!(!h["User-Agent"].to_str().unwrap().contains("978.0.7"));

        let json = data(WINDOWS).grandslam_headers(true).unwrap();
        assert_eq!(json["Content-Type"], "application/json");
        assert_eq!(json["Accept"], "application/json");
    }

    #[test]
    fn client_info_stays_out_of_the_cpd() {
        let d = data(WINDOWS);
        let cpd = d.to_plist(true, false, false);
        assert!(cpd.get("X-Mme-Client-Info").is_none());
        assert_eq!(cpd.get("svct").and_then(|v| v.as_string()), Some("iCloud"));
        assert_eq!(cpd.get("X-Apple-I-MD").and_then(|v| v.as_string()), Some("Mixed/Case+OTP=="));
        // asked explicitly (developer portal): client info and app info are there
        let h = d.generate_headers(false, true, true);
        assert_eq!(h["X-Mme-Client-Info"], WINDOWS);
        assert_eq!(h["X-Xcode-Version"], XCODE_VERSION);
    }

    #[test]
    fn missing_client_info_is_an_error_not_a_panic() {
        let mut d = data(WINDOWS);
        d.base_headers.remove("X-Mme-Client-Info");
        assert!(d.client_info().is_err());
        assert!(d.grandslam_headers(false).is_err());
    }

    #[test]
    fn age_never_panics() {
        let mut d = data(WINDOWS);
        assert!(!d.needs_refresh() && d.is_valid());
        d.generated_at = SystemTime::now() + std::time::Duration::from_secs(3600);
        assert!(!d.needs_refresh() && d.is_valid());
        d.generated_at = SystemTime::now() - std::time::Duration::from_secs(100);
        assert!(d.needs_refresh() && !d.is_valid());
    }

    #[test]
    fn no_backoff_before_any_failure() {
        assert!(!data(WINDOWS).in_backoff(Instant::now()));
    }

    #[test]
    fn a_failed_refresh_pauses_new_attempts_for_a_while() {
        let mut d = data(WINDOWS);
        let failed = Instant::now();
        d.refresh_failed_at = Some(failed);
        assert!(d.in_backoff(failed + Duration::from_secs(5)));
        assert!(d.in_backoff(failed + REFRESH_BACKOFF - Duration::from_secs(1)));
        assert!(!d.in_backoff(failed + REFRESH_BACKOFF));
    }
}
