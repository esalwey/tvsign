use std::str::FromStr;

use crate::Error;
use base64::{Engine, engine::general_purpose};
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};

use crate::auth::account::{send_with_retry, snippet};
use crate::auth::{
    Account, AuthenticationExtras, LoginState, PhoneNumber, VerifyBody, VerifyCode, gsa_url,
};

/// What Apple says (HTTP 403) when it refuses to send or check a code by SMS.
const SMS_DENIED_MESSAGE: &str = "Access to this Apple Developer Service was denied";

/// The error for an SMS endpoint that answered `status` with `body`: HTTP 403 means Apple does not
/// serve SMS codes to this client (trusted-device codes still work).
fn sms_refusal(status: u16, body: &str) -> Option<Error> {
    if status != 403 {
        return None;
    }
    let body = snippet(body);
    let message = if body.is_empty() {
        SMS_DENIED_MESSAGE.to_string()
    } else if body.contains(SMS_DENIED_MESSAGE) {
        body
    } else {
        format!("{SMS_DENIED_MESSAGE} ({body})")
    };
    Some(Error::SmsRefused { status, message })
}

impl Account {
    pub async fn send_2fa_to_devices(&self) -> Result<LoginState, Error> {
        let headers = self.build_2fa_headers(false).await?;

        let res = send_with_retry(|| {
            self.client
                .get(&gsa_url("/auth/verify/trusteddevice"))
                .headers(headers.clone())
        })
        .await?;

        let status_code = res.status();

        // 409: a code was just pushed to the trusted devices (by the login itself): use that one.
        if status_code.as_u16() == 409 {
            return Ok(LoginState::Needs2FAVerification);
        }

        if !status_code.is_success() {
            return Err(Error::AuthSrpWithMessage(
                status_code.as_u16() as i64,
                "Failed to send 2FA to devices".to_string(),
            ));
        }

        return Ok(LoginState::Needs2FAVerification);
    }

    pub async fn send_sms_2fa_to_devices(&self, phone_id: u32) -> Result<LoginState, Error> {
        let headers = self.build_2fa_headers(true).await?;

        let body = VerifyBody {
            phone_number: PhoneNumber { id: phone_id },
            mode: "sms".to_string(),
            security_code: None,
        };

        let res = send_with_retry(|| {
            self.client
                .put(&gsa_url("/auth/verify/phone"))
                .headers(headers.clone())
                .json(&body)
        })
        .await?;

        let status_code = res.status();

        if !status_code.is_success() {
            let text = res.text().await.unwrap_or_default();
            if let Some(refused) = sms_refusal(status_code.as_u16(), &text) {
                return Err(refused);
            }
            return Err(Error::AuthSrpWithMessage(
                status_code.as_u16() as i64,
                "Failed to send SMS 2FA to devices".to_string(),
            ));
        }

        return Ok(LoginState::NeedsSMS2FAVerification(body));
    }

    pub async fn get_auth_extras(&self) -> Result<AuthenticationExtras, Error> {
        let headers = self.build_2fa_headers(true).await?;

        let req = send_with_retry(|| {
            self.client
                .get(&gsa_url("/auth"))
                .headers(headers.clone())
                .header("Accept", "application/json")
        })
        .await?;
        let status = req.status().as_u16();
        let mut new_state = req.json::<AuthenticationExtras>().await?;
        if status == 201 {
            if let Some(first) = new_state.trusted_phone_numbers.first() {
                new_state.new_state = Some(LoginState::NeedsSMS2FAVerification(VerifyBody {
                    phone_number: PhoneNumber { id: first.id },
                    mode: "sms".to_string(),
                    security_code: None,
                }));
            }
        }

        Ok(new_state)
    }

    pub async fn verify_2fa(&self, code: String) -> Result<LoginState, Error> {
        let headers = self.build_2fa_headers(false).await?;
        let code = HeaderValue::from_str(&code).map_err(|_| Error::Bad2faCode)?;
        let res = send_with_retry(|| {
            self.client
                .get(&gsa_url("/grandslam/GsService2/validate"))
                .headers(headers.clone())
                .header(HeaderName::from_str("security-code").unwrap(), code.clone())
        })
        .await?;

        let status = res.status().as_u16();
        let text = res.text().await?;
        let start = text.trim_start();
        if !start.starts_with("<?xml") && !start.starts_with("<plist") {
            return Err(Error::GsaHttp {
                status,
                body: snippet(&text),
            });
        }
        let res: plist::Dictionary = plist::from_bytes(text.as_bytes())?;

        super::check_error(&res)?;

        Ok(LoginState::NeedsLogin)
    }

    pub async fn verify_sms_2fa(
        &self,
        code: String,
        mut body: VerifyBody,
    ) -> Result<LoginState, Error> {
        let headers = self.build_2fa_headers(true).await?;
        body.security_code = Some(VerifyCode { code });
        let res = send_with_retry(|| {
            self.client
                .post(&gsa_url("/auth/verify/phone/securitycode"))
                .headers(headers.clone())
                .json(&body)
        })
        .await?;

        let status_code = res.status();

        // TODO: 423 http code may occur, in this case we to ask for sending
        // last code sent (unlikely it would even work), or try again later
        if !status_code.is_success() {
            let text = res.text().await.unwrap_or_default();
            if let Some(refused) = sms_refusal(status_code.as_u16(), &text) {
                return Err(refused);
            }
            return Err(Error::Bad2faCode);
        }

        Ok(LoginState::NeedsLogin)
    }

    /// Headers of the 2FA requests (GET `trusteddevice`, `validate`, `/auth`, SMS endpoints): the
    /// device the anisette data belongs to (client info + user agent of the anisette server), the
    /// anisette data, and the identity token of the session.
    async fn build_2fa_headers(&self, sms: bool) -> Result<HeaderMap, Error> {
        let spd = self.spd.as_ref().ok_or(Error::Parse)?;
        let dsid = spd
            .get("adsid")
            .and_then(plist::Value::as_string)
            .ok_or(Error::Parse)?;
        let token = spd
            .get("GsIdmsToken")
            .and_then(plist::Value::as_string)
            .ok_or(Error::Parse)?;

        let identity_token = general_purpose::STANDARD.encode(format!("{}:{}", dsid, token));

        let valid_anisette = self.get_anisette().await?;
        let mut headers = valid_anisette.grandslam_headers(sms)?;
        for (k, v) in valid_anisette.generate_headers(false, false, false) {
            if let (Ok(name), Ok(value)) = (
                HeaderName::from_bytes(k.as_bytes()),
                HeaderValue::from_str(&v),
            ) {
                headers.insert(name, value);
            }
        }
        headers.insert("Accept-Language", HeaderValue::from_static("en-us"));
        headers.append(
            "X-Apple-Identity-Token",
            HeaderValue::from_str(&identity_token).map_err(|_| Error::Parse)?,
        );

        if let Ok(locale) = valid_anisette.get_header("x-apple-locale") {
            if let Ok(value) = HeaderValue::from_str(&locale) {
                headers.insert("Loc", value);
            }
        }

        Ok(headers)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn http_403_on_the_sms_endpoints_is_a_clear_refusal() {
        let refused = sms_refusal(403, "").unwrap();
        let text = refused.to_string();
        assert!(matches!(refused, Error::SmsRefused { status: 403, .. }));
        assert!(text.contains("SMS 2FA refused by Apple"), "{text}");
        assert!(text.contains(SMS_DENIED_MESSAGE), "{text}");
        assert!(text.contains("trusted Apple device"), "{text}");
    }

    #[test]
    fn the_body_of_the_refusal_is_kept_when_it_is_text() {
        let text = sms_refusal(403, "{\"error\":\"nope\"}").unwrap().to_string();
        assert!(text.contains("nope"), "{text}");
        // the standard sentence is always there, whatever the body says
        let html = sms_refusal(403, "<html><head><title>Forbidden</title></head></html>")
            .unwrap()
            .to_string();
        assert!(html.contains(SMS_DENIED_MESSAGE) && html.contains("Forbidden"), "{html}");
        // Apple's own sentence is not repeated
        let apple = sms_refusal(403, SMS_DENIED_MESSAGE).unwrap().to_string();
        assert_eq!(apple.matches(SMS_DENIED_MESSAGE).count(), 1, "{apple}");
    }

    #[test]
    fn other_statuses_are_not_a_refusal() {
        for status in [200, 400, 401, 412, 423, 429, 500, 503] {
            assert!(sms_refusal(status, "x").is_none(), "{status}");
        }
    }
}
