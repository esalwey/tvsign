use omnisette::AnisetteConfiguration;
use plist::{Dictionary, Value};
use reqwest::Client;
use sha2::{Digest, Sha256};
use srp::groups::G2048;

use crate::Error;

use crate::auth::account::{
    check_error, parse_response, resync_sim, send_with_retry, status_hsc,
};
use crate::auth::anisette_data::AnisetteData;
use crate::auth::{
    Account, ChallengeRequest, ChallengeRequestBody, GSA_SERVICE, InitRequest, InitRequestBody,
    LoginState, RequestHeader, TrustedPhoneNumber, TwoFactorAction, TwoFactorMethod,
    TwoFactorRequest, gsa_url,
};

#[macro_export]
macro_rules! plist_get_string {
    ($base:expr, $( $path:literal )+, $final_key:literal) => {{
        let mut current_val = $base;
        $(
            current_val = current_val
                .get($path)
                .expect(concat!("Missing dictionary key: ", $path))
                .as_dictionary()
                .expect(concat!("Key value is not a dictionary: ", $path));
        )+
        current_val
            .get($final_key)
            .expect(concat!("Missing string key: ", $final_key))
            .as_string()
            .expect(concat!("Value is not a string: ", $final_key))
            .to_string()
    }};

    ($base:expr, $key:literal) => {{
        $base
            .get($key)
            .expect(concat!("Missing key: ", $key))
            .as_string()
            .expect(concat!("Value is not a string: ", $key))
            .to_string()
    }};
}

/// Steps (2FA prompts, new logins) after which a conversation that does not end is given up.
const MAX_LOGIN_STEPS: u8 = 15;
/// Prompts for a code after which Apple is considered to be asking in a loop (a code that was
/// accepted, then a new request for a code, again and again).
const MAX_CODE_PROMPTS: u8 = 4;

/// The password as the SRP protocol that Apple selected wants it: `s2k` the SHA-256 of the password,
/// `s2k_fo` the lower-case hex text of that hash. Both were offered in the `init` request.
pub(crate) fn password_for_protocol(protocol: &str, hashed_password: &[u8]) -> Result<Vec<u8>, Error> {
    match protocol {
        "s2k" => Ok(hashed_password.to_vec()),
        "s2k_fo" => Ok(hex::encode(hashed_password).into_bytes()),
        other => Err(Error::AuthSrpWithMessage(
            0,
            format!("Unsupported SRP protocol selected by Apple: {other}"),
        )),
    }
}

/// A code typed as `123 456` or `123-456` is the code `123456`.
pub(crate) fn normalize_code(code: &str) -> String {
    code.chars().filter(|c| c.is_ascii_alphanumeric()).collect()
}

/// What the end of the "complete" step says about two-factor authentication.
pub(crate) fn state_after_complete(status: &Dictionary) -> LoginState {
    if let Some(Value::String(auth_type)) = status.get("au") {
        return match auth_type.as_str() {
            "trustedDeviceSecondaryAuth" => LoginState::NeedsDevice2FA,
            "secondaryAuth" => LoginState::NeedsSMS2FA,
            other => LoginState::NeedsExtraStep(other.to_string()),
        };
    }
    // hsc 409 and no `au`: Apple has already pushed the code to the trusted devices. Asking for
    // another push would only send a second code: go straight to the entry of the code.
    if status
        .get("hsc")
        .and_then(Value::as_signed_integer)
        .is_some_and(|hsc| hsc == 409)
    {
        return LoginState::Needs2FAVerification;
    }
    LoginState::LoggedIn
}

/// The "init" request of the SRP login: sends the public ephemeral and the anisette data.
async fn send_init(
    client: &Client,
    anisette: &AnisetteData,
    a_pub: &[u8],
    username: &str,
) -> Result<Dictionary, Error> {
    let init_body = InitRequestBody {
        a_pub: plist::Value::Data(a_pub.to_vec()),
        cpd: anisette.to_plist(true, false, false),
        operation: "init".to_string(),
        ps: vec!["s2k".to_string(), "s2k_fo".to_string()],
        username: username.to_string(),
    };

    let init_packet = InitRequest {
        header: RequestHeader {
            version: "1.0.1".to_string(),
        },
        request: init_body,
    };

    let mut buffer = Vec::new();
    plist::to_writer_xml(&mut buffer, &init_packet)?;

    let headers = anisette.grandslam_headers(false)?;
    let url = gsa_url(GSA_SERVICE);
    let res = send_with_retry(|| {
        client
            .post(&url)
            .headers(headers.clone())
            .body(buffer.clone())
    })
    .await;

    parse_response(res).await
}

impl AnisetteData {
    /// An anisette identity that was just provisioned has never been seen by Apple: its first
    /// GrandSlam request is answered with hsc 434 and the data to synchronize with. A sign-in does
    /// that on its own; the stored sessions of a renewal never talk to GrandSlam, so this asks once
    /// with a throwaway Apple ID (no account is touched) and synchronizes, so that the first request
    /// to the developer portal carries data Apple has already seen. Best effort: the caller keeps
    /// its data when this fails.
    pub async fn synchronize_with_apple(&self) -> Result<AnisetteData, Error> {
        let client = crate::client()?;
        let username = format!("tvsign-sync-{:016x}@example.invalid", rand::random::<u64>());
        let srp_client = srp::Client::<G2048, Sha256>::new_with_options(false);
        let a: Vec<u8> = (0..32).map(|_| rand::random::<u8>()).collect();
        let a_pub = srp_client.compute_public_ephemeral(&a);

        let res = send_init(&client, self, &a_pub, &username).await?;
        match resync_sim(&res) {
            Some(sim) => {
                log::info!("Apple asked the new anisette identity to synchronize");
                self.resync(&sim).await
            }
            None => Ok(self.clone()),
        }
    }
}

impl Account {
    pub async fn login(
        appleid_closure: impl Fn() -> Result<(String, String), String>,
        tfa_closure: impl Fn(TwoFactorRequest) -> Result<TwoFactorAction, String>,
        config: AnisetteConfiguration,
    ) -> Result<Account, Error> {
        let anisette = AnisetteData::new(config).await?;
        Account::login_with_anisette(appleid_closure, tfa_closure, anisette).await
    }

    pub async fn login_with_anisette<
        F: Fn() -> Result<(String, String), String>,
        G: Fn(TwoFactorRequest) -> Result<TwoFactorAction, String>,
    >(
        appleid_closure: F,
        tfa_closure: G,
        anisette: AnisetteData,
    ) -> Result<Account, Error> {
        let mut _self = Account::new_with_anisette(anisette)?;
        let (username, password) = appleid_closure().map_err(|e| {
            Error::AuthSrpWithMessage(0, format!("Failed to get Apple ID credentials: {}", e))
        })?;

        let mut response = _self.login_email_pass(&username, &password).await?;
        // Cached so the caller can be offered an SMS fallback even when a trusted
        // device handled the push. Fetched lazily the first time 2FA is needed.
        let mut trusted_phone_numbers: Vec<TrustedPhoneNumber> = Vec::new();
        let mut numbers_fetched = false;
        // Apple refuses the codes by SMS (HTTP 403 since October 2026): offered no more once it did.
        let mut sms_refused = false;
        let mut steps: u8 = 0;
        let mut prompts: u8 = 0;

        loop {
            steps += 1;
            if steps > MAX_LOGIN_STEPS {
                return Err(Error::AuthSrpWithMessage(
                    0,
                    "Apple keeps asking for a verification: sign-in abandoned".to_string(),
                ));
            }
            match response {
                LoginState::NeedsDevice2FA => {
                    response = _self.send_2fa_to_devices().await?;
                    if !numbers_fetched {
                        numbers_fetched = true;
                        if let Ok(extras) = _self.get_auth_extras().await {
                            trusted_phone_numbers = extras.trusted_phone_numbers;
                        }
                    }
                }
                LoginState::Needs2FAVerification => {
                    if !numbers_fetched {
                        numbers_fetched = true;
                        if let Ok(extras) = _self.get_auth_extras().await {
                            trusted_phone_numbers = extras.trusted_phone_numbers;
                        }
                    }
                    prompts += 1;
                    if prompts > MAX_CODE_PROMPTS {
                        return Err(Error::AuthSrpWithMessage(
                            0,
                            "Apple keeps asking for a verification code: sign-in abandoned".to_string(),
                        ));
                    }
                    let request = TwoFactorRequest {
                        method: TwoFactorMethod::Device,
                        trusted_phone_numbers: if sms_refused {
                            Vec::new()
                        } else {
                            trusted_phone_numbers.clone()
                        },
                        sms_refused,
                    };
                    match tfa_closure(request).map_err(|e| {
                        Error::AuthSrpWithMessage(0, format!("Failed to get 2FA code: {}", e))
                    })? {
                        TwoFactorAction::SubmitCode(code) => {
                            response = _self.verify_2fa(normalize_code(&code)).await?
                        }
                        TwoFactorAction::SendSms(id) => {
                            match _self.send_sms_2fa_to_devices(id).await {
                                Ok(next) => response = next,
                                Err(Error::SmsRefused { status, .. }) => {
                                    // The code is still on the trusted devices: ask for it again.
                                    log::warn!(
                                        "Apple refuses the code by SMS (HTTP {status}): enter the code shown on a trusted Apple device"
                                    );
                                    sms_refused = true;
                                    response = LoginState::Needs2FAVerification;
                                }
                                Err(e) => return Err(e),
                            }
                        }
                    }
                }
                LoginState::NeedsSMS2FA => {
                    if !numbers_fetched {
                        numbers_fetched = true;
                        if let Ok(extras) = _self.get_auth_extras().await {
                            trusted_phone_numbers = extras.trusted_phone_numbers;
                        }
                    }
                    let id = trusted_phone_numbers.first().map(|p| p.id).unwrap_or(1);
                    response = _self.send_sms_2fa_to_devices(id).await?;
                }
                LoginState::NeedsSMS2FAVerification(body) => {
                    let request = TwoFactorRequest {
                        method: TwoFactorMethod::Sms,
                        trusted_phone_numbers: trusted_phone_numbers.clone(),
                        sms_refused: false,
                    };
                    match tfa_closure(request).map_err(|e| {
                        Error::AuthSrpWithMessage(
                            0,
                            format!("Failed to get SMS 2FA code: {}", e),
                        )
                    })? {
                        TwoFactorAction::SubmitCode(code) => {
                            response = _self.verify_sms_2fa(normalize_code(&code), body).await?
                        }
                        TwoFactorAction::SendSms(id) => {
                            response = _self.send_sms_2fa_to_devices(id).await?
                        }
                    }
                }
                LoginState::NeedsLogin => {
                    response = _self.login_email_pass(&username, &password).await?
                }
                LoginState::LoggedIn => return Ok(_self),
                LoginState::NeedsExtraStep(step) => {
                    if _self.get_pet().is_some() {
                        return Ok(_self);
                    } else {
                        return Err(Error::ExtraStep(step));
                    }
                }
            }
        }
    }

    pub async fn login_email_pass(
        &mut self,
        username: &str,
        password: &str,
    ) -> Result<LoginState, Error> {
        let username_for_spd = username.to_string().to_lowercase();
        let srp_client = srp::Client::<G2048, Sha256>::new_with_options(false);
        let a: Vec<u8> = (0..32).map(|_| rand::random::<u8>()).collect();
        let a_pub = srp_client.compute_public_ephemeral(&a);

        let mut anisette = self.get_anisette().await?;
        let header = RequestHeader {
            version: "1.0.1".to_string(),
        };

        // The first answer may ask for something before the login proper: hsc 434 (synchronize the
        // anisette identity with Apple, once) or -45061 (the provisioning is not valid: provision
        // again, once).
        let mut resynced = false;
        let mut reprovisioned = false;
        let res = loop {
            let res = send_init(&self.client, &anisette, &a_pub, &username_for_spd).await?;
            if !resynced && let Some(sim) = resync_sim(&res) {
                resynced = true;
                log::info!("Apple asks the anisette identity to synchronize (hsc 434)");
                anisette = self.resync_anisette(&sim).await?;
                continue;
            }
            match check_error(&res) {
                Err(Error::AuthSrpWithMessage(-45061, _)) if !reprovisioned => {
                    reprovisioned = true;
                    log::warn!("Apple answered -45061: provisioning the anisette identity again");
                    anisette = self.reprovision_anisette().await?;
                }
                other => {
                    other?;
                    break res;
                }
            }
        };

        let salt = res.get("s").and_then(Value::as_data).ok_or(Error::Parse)?;
        let b_pub = res.get("B").and_then(Value::as_data).ok_or(Error::Parse)?;
        let iters = res
            .get("i")
            .and_then(Value::as_signed_integer)
            .ok_or(Error::Parse)?;
        let c = res.get("c").and_then(Value::as_string).ok_or(Error::Parse)?;

        let hashed_password = Sha256::digest(password.as_bytes());
        let protocol = res.get("sp").and_then(Value::as_string).unwrap_or("s2k");
        let password_hash = password_for_protocol(protocol, &hashed_password)?;

        let mut password_buf = [0u8; 32];
        pbkdf2::pbkdf2::<hmac::Hmac<Sha256>>(
            &password_hash,
            salt,
            iters as u32,
            &mut password_buf,
        )?;

        let verifier = srp_client
            .process_reply(&a, username_for_spd.as_bytes(), &password_buf, salt, b_pub)
            .map_err(|_| {
                Error::AuthSrpWithMessage(0, "SRP negotiation failed (invalid parameters from Apple)".to_string())
            })?;

        let challenge_body = ChallengeRequestBody {
            m: plist::Value::Data(verifier.proof().to_vec()),
            c: c.to_string(),
            cpd: anisette.to_plist(true, false, false),
            operation: "complete".to_string(),
            username: username_for_spd.clone(),
        };

        let challenge_packet = ChallengeRequest {
            header,
            request: challenge_body,
        };

        let mut buffer = Vec::new();
        plist::to_writer_xml(&mut buffer, &challenge_packet)?;

        let headers = anisette.grandslam_headers(false)?;
        let url = gsa_url(GSA_SERVICE);
        let res = send_with_retry(|| {
            self.client
                .post(&url)
                .headers(headers.clone())
                .body(buffer.clone())
        })
        .await;

        let res = parse_response(res).await?;
        check_error(&res)?;

        let m2 = res.get("M2").and_then(Value::as_data).ok_or(Error::Parse)?;
        verifier.verify_server(m2).map_err(|_| {
            Error::AuthSrpWithMessage(0, "SRP negotiation failed (server proof mismatch)".to_string())
        })?;

        let spd_encrypted = res.get("spd").and_then(Value::as_data).ok_or(Error::Parse)?;
        let spd_decrypted = super::decrypt_cbc(&verifier, spd_encrypted);
        let mut spd: Dictionary = plist::from_bytes(&spd_decrypted)?;

        if !spd.contains_key("appleId") {
            spd.insert(
                "appleId".to_string(),
                plist::Value::String(username_for_spd),
            );
        }

        self.spd = Some(spd);

        let status = res
            .get("Status")
            .and_then(Value::as_dictionary)
            .ok_or(Error::Parse)?;
        if let Some(hsc) = status_hsc(&res) {
            log::debug!("login: Status.hsc = {hsc}");
        }
        Ok(state_after_complete(status))
    }

    pub fn get_pet(&self) -> Option<String> {
        let base = self.spd.as_ref().unwrap();
        let token = base.get("t")?.as_dictionary()?;

        Some(plist_get_string!(token, "com.apple.gs.idms.pet", "token"))
    }

    pub fn get_name(&self) -> (String, String) {
        let base = self.spd.as_ref().unwrap();
        (plist_get_string!(base, "fn"), plist_get_string!(base, "ln"))
    }

    /// The anisette data of this account, fetched again when they are older than a minute.
    pub async fn get_anisette(&self) -> Result<AnisetteData, Error> {
        let mut locked = self.anisette.lock().await;
        if locked.needs_refresh() {
            *locked = locked.refresh().await?;
        }
        Ok(locked.clone())
    }

    /// Synchronize the anisette identity with Apple (hsc 434) and use the new data from now on.
    async fn resync_anisette(&self, sim: &str) -> Result<AnisetteData, Error> {
        let mut locked = self.anisette.lock().await;
        let fresh = locked.resync(sim).await?;
        *locked = fresh.clone();
        Ok(fresh)
    }

    /// Provision the anisette identity again (Apple answered -45061) and use the new data.
    async fn reprovision_anisette(&self) -> Result<AnisetteData, Error> {
        let mut locked = self.anisette.lock().await;
        omnisette::invalidate_provisioning(locked.config.configuration_path())?;
        let fresh = AnisetteData::new(locked.config.clone()).await?;
        *locked = fresh.clone();
        Ok(fresh)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::auth::account::parse_gsa_text;

    fn status(xml: &str) -> Dictionary {
        let text = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>Response</key><dict><key>Status</key><dict>{xml}</dict></dict></dict></plist>"
        );
        parse_gsa_text(200, &text)
            .unwrap()
            .get("Status")
            .and_then(Value::as_dictionary)
            .unwrap()
            .clone()
    }

    #[test]
    fn hsc_409_without_au_goes_straight_to_the_code() {
        // no push is requested again: NeedsDevice2FA would send a second code
        assert!(matches!(
            state_after_complete(&status("<key>hsc</key><integer>409</integer>")),
            LoginState::Needs2FAVerification
        ));
    }

    #[test]
    fn au_decides_before_hsc() {
        assert!(matches!(
            state_after_complete(&status(
                "<key>au</key><string>trustedDeviceSecondaryAuth</string><key>hsc</key><integer>409</integer>"
            )),
            LoginState::NeedsDevice2FA
        ));
        assert!(matches!(
            state_after_complete(&status("<key>au</key><string>secondaryAuth</string>")),
            LoginState::NeedsSMS2FA
        ));
        assert!(matches!(
            state_after_complete(&status("<key>au</key><string>repair</string>")),
            LoginState::NeedsExtraStep(s) if s == "repair"
        ));
    }

    #[test]
    fn the_selected_srp_protocol_decides_how_the_password_is_fed() {
        let hash = Sha256::digest(b"password");
        assert_eq!(password_for_protocol("s2k", &hash).unwrap(), hash.to_vec());
        let hex = password_for_protocol("s2k_fo", &hash).unwrap();
        assert_eq!(hex.len(), 64);
        assert_eq!(
            String::from_utf8(hex).unwrap(),
            "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"
        );
        assert!(password_for_protocol("s3k", &hash).is_err());
    }

    #[test]
    fn a_code_is_reduced_to_its_characters() {
        assert_eq!(normalize_code("123456"), "123456");
        assert_eq!(normalize_code(" 123 456\n"), "123456");
        assert_eq!(normalize_code("123-456"), "123456");
        assert_eq!(normalize_code(""), "");
    }

    #[test]
    fn normal_answers_are_logged_in() {
        assert!(matches!(
            state_after_complete(&status("<key>hsc</key><integer>200</integer>")),
            LoginState::LoggedIn
        ));
        assert!(matches!(state_after_complete(&status("")), LoginState::LoggedIn));
        assert!(matches!(
            state_after_complete(&status("<key>hsc</key><integer>433</integer>")),
            LoginState::LoggedIn
        ));
    }
}
