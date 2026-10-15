mod login;
mod token;
mod two_factor_auth;

#[cfg(test)]
mod flow_tests;

use std::time::Duration;

use aes::cipher::BlockModeDecrypt;
use cbc::cipher::{KeyIvInit, block_padding::Pkcs7};
use hmac::{Hmac, KeyInit, Mac};
use reqwest::{RequestBuilder, Response};
use sha2::Sha256;
use srp::ClientVerifier;

use crate::Error;

/// Retries of a GrandSlam request that was shed (HTTP 429/502/503/504), after the first try.
const GSA_RETRIES: u32 = 3;

/// The edge in front of GrandSlam answers `503 Service Temporarily Unavailable` (HTML) to requests it
/// refuses: anisette data or a client info it does not accept, but also, in bursts, perfectly
/// good requests while the shared anisette machines are busy. 429 asks to slow down.
pub(crate) fn is_retryable_status(status: u16) -> bool {
    matches!(status, 429 | 502 | 503 | 504)
}

/// Pause before retry number `attempt` (0 for the first retry): 2 s, 4 s, 6 s.
pub(crate) fn retry_delay(attempt: u32) -> Duration {
    Duration::from_secs(2 * (u64::from(attempt) + 1))
}

/// Send a GrandSlam request, trying again a few times when the answer says "not now".
/// `make` builds the request anew for each try. A server that keeps refusing is reported by the
/// caller from the last answer.
pub(crate) async fn send_with_retry(
    make: impl Fn() -> RequestBuilder,
) -> Result<Response, reqwest::Error> {
    let mut attempt = 0;
    loop {
        let res = make().send().await?;
        let status = res.status().as_u16();
        if !is_retryable_status(status) || attempt >= GSA_RETRIES {
            return Ok(res);
        }
        let wait = res
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.trim().parse::<u64>().ok())
            .map(|secs| Duration::from_secs(secs.min(30)))
            .unwrap_or_else(|| retry_delay(attempt));
        // worded so that it is not taken for the final refusal when something else fails later
        log::warn!(
            "GrandSlam is busy (HTTP {status}): trying again in {} s ({}/{GSA_RETRIES})",
            wait.as_secs(),
            attempt + 1
        );
        tokio::time::sleep(wait).await;
        attempt += 1;
    }
}

pub async fn parse_response(
    res: Result<Response, reqwest::Error>,
) -> Result<plist::Dictionary, Error> {
    let res = res?;
    let status = res.status();
    let text = res.text().await?;
    parse_gsa_text(status.as_u16(), &text)
}

/// The `Response` dictionary of a GrandSlam answer. An answer that is not a plist (the HTML
/// "503 Service Temporarily Unavailable" page Apple sends for anisette data or a client info it
/// refuses) is an error that says so, not a parse failure or a panic.
pub(crate) fn parse_gsa_text(status: u16, text: &str) -> Result<plist::Dictionary, Error> {
    let start = text.trim_start();
    if !start.starts_with("<?xml") && !start.starts_with("<plist") {
        let body = snippet(text);
        log::debug!("no plist in the answer of GrandSlam (HTTP {status}): {body}");
        return Err(Error::GsaHttp { status, body });
    }
    let res: plist::Dictionary = plist::from_bytes(text.as_bytes())?;
    match res.get("Response") {
        Some(plist::Value::Dictionary(dict)) => Ok(dict.clone()),
        _ => Err(Error::Parse),
    }
}

/// First characters of an answer that is not a plist: the `<title>` of an HTML page, else the text
/// itself on one line.
pub(crate) fn snippet(text: &str) -> String {
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

pub fn check_error(res: &plist::Dictionary) -> Result<(), Error> {
    let res = match res.get("Status") {
        Some(plist::Value::Dictionary(d)) => d,
        _ => &res,
    };

    let ec = res
        .get("ec")
        .and_then(plist::Value::as_signed_integer)
        .unwrap_or(0);
    if ec != 0 {
        let em = res
            .get("em")
            .and_then(plist::Value::as_string)
            .unwrap_or_default()
            .to_owned();
        return Err(Error::AuthSrpWithMessage(ec, em));
    }

    Ok(())
}

/// `hsc` of the `Status` of a GrandSlam answer.
pub(crate) fn status_hsc(res: &plist::Dictionary) -> Option<i64> {
    res.get("Status")?
        .as_dictionary()?
        .get("hsc")
        .and_then(plist::Value::as_signed_integer)
}

/// The synchronization data (`X-Apple-I-MD-DATA`, base64) of an answer that asks for a resync
/// (`Status.hsc` 434: the anisette identity is new to Apple, or out of sync); `None` otherwise.
pub(crate) fn resync_sim(res: &plist::Dictionary) -> Option<String> {
    use base64::{Engine, engine::general_purpose::STANDARD};

    if status_hsc(res)? != 434 {
        return None;
    }
    match res.get("Status")?.as_dictionary()?.get("X-Apple-I-MD-DATA")? {
        plist::Value::Data(data) => Some(STANDARD.encode(data)),
        plist::Value::String(sim) => Some(sim.clone()),
        _ => None,
    }
}

pub fn decrypt_cbc(usr: &ClientVerifier<Sha256>, data: &[u8]) -> Vec<u8> {
    let extra_data_key = create_session_key(usr, "extra data key:");
    let extra_data_iv = create_session_key(usr, "extra data iv:");
    let extra_data_iv = &extra_data_iv[..16];

    cbc::Decryptor::<aes::Aes256>::new_from_slices(&extra_data_key, extra_data_iv)
        .unwrap()
        .decrypt_padded_vec::<Pkcs7>(&data)
        .unwrap()
}

pub fn create_session_key(usr: &ClientVerifier<Sha256>, name: &str) -> Vec<u8> {
    Hmac::<Sha256>::new_from_slice(&usr.key())
        .unwrap()
        .chain_update(name.as_bytes())
        .finalize()
        .into_bytes()
        .to_vec()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plist_of(xml_status: &str) -> String {
        format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict><key>Response</key><dict><key>Status</key><dict>{xml_status}</dict></dict></dict></plist>"
        )
    }

    #[test]
    fn shed_requests_are_retried_refusals_are_not() {
        for status in [429, 502, 503, 504] {
            assert!(is_retryable_status(status), "{status}");
        }
        for status in [200, 201, 400, 401, 403, 404, 409, 412, 500] {
            assert!(!is_retryable_status(status), "{status}");
        }
        assert_eq!(retry_delay(0), Duration::from_secs(2));
        assert_eq!(retry_delay(1), Duration::from_secs(4));
        assert_eq!(retry_delay(2), Duration::from_secs(6));
        // at most 2 + 4 + 6 s of waiting in total
        let total: u64 = (0..GSA_RETRIES).map(|a| retry_delay(a).as_secs()).sum();
        assert_eq!(total, 12);
    }

    /// Local HTTP server answering with `statuses` in turn (the last one repeats), `Retry-After: 0`
    /// on every answer; returns its URL and the number of requests received.
    fn serve(statuses: Vec<u16>) -> (String, std::sync::Arc<std::sync::atomic::AtomicUsize>) {
        use std::io::{BufRead, BufReader, Write};
        use std::sync::atomic::{AtomicUsize, Ordering};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let hits = std::sync::Arc::new(AtomicUsize::new(0));
        let counter = hits.clone();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { continue };
                let n = counter.fetch_add(1, Ordering::SeqCst);
                let status = *statuses.get(n).or(statuses.last()).unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                }
                let _ = write!(
                    stream,
                    "HTTP/1.1 {status} X\r\nRetry-After: 0\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                );
            }
        });
        (url, hits)
    }

    fn block_on<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(future)
    }

    #[test]
    fn a_shed_request_is_sent_again_until_it_goes_through() {
        use std::sync::atomic::Ordering;
        let (url, hits) = serve(vec![503, 503, 200]);
        let client = reqwest::Client::new();
        let res = block_on(send_with_retry(|| client.get(&url))).unwrap();
        assert_eq!(res.status().as_u16(), 200);
        assert_eq!(hits.load(Ordering::SeqCst), 3);
    }

    #[test]
    fn a_refusal_that_lasts_is_reported_after_the_last_try() {
        use std::sync::atomic::Ordering;
        let (url, hits) = serve(vec![503]);
        let client = reqwest::Client::new();
        let res = block_on(send_with_retry(|| client.get(&url))).unwrap();
        assert_eq!(res.status().as_u16(), 503);
        assert_eq!(hits.load(Ordering::SeqCst), 1 + GSA_RETRIES as usize);
    }

    #[test]
    fn a_real_refusal_is_not_repeated() {
        use std::sync::atomic::Ordering;
        for status in [403, 409, 412] {
            let (url, hits) = serve(vec![status]);
            let client = reqwest::Client::new();
            let res = block_on(send_with_retry(|| client.get(&url))).unwrap();
            assert_eq!(res.status().as_u16(), status);
            assert_eq!(hits.load(Ordering::SeqCst), 1, "{status}");
        }
    }

    #[test]
    fn html_503_page_is_a_clear_error() {
        let page = "<html>\r\n<head><title>503 Service Temporarily Unavailable</title></head>\r\n<body><center><h1>503 Service Temporarily Unavailable</h1></center><hr><center>Apple</center></body></html>";
        let error = parse_gsa_text(503, page).unwrap_err();
        assert!(matches!(error, Error::GsaHttp { status: 503, .. }));
        let text = error.to_string();
        assert!(text.contains("HTTP 503") && text.contains("503 Service Temporarily Unavailable"), "{text}");
    }

    #[test]
    fn empty_and_odd_bodies_do_not_panic() {
        assert!(matches!(parse_gsa_text(403, ""), Err(Error::GsaHttp { status: 403, .. })));
        assert!(matches!(parse_gsa_text(200, "{\"json\": true}"), Err(Error::GsaHttp { .. })));
        // multi-byte characters around the cut of a long body
        let long = "é".repeat(1000);
        assert!(parse_gsa_text(500, &long).is_err());
        // a plist without Response
        let no_response = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict></dict></plist>";
        assert!(matches!(parse_gsa_text(200, no_response), Err(Error::Parse)));
    }

    #[test]
    fn apple_errors_keep_code_and_message() {
        let res = parse_gsa_text(
            200,
            &plist_of("<key>ec</key><integer>-80045</integer><key>em</key><string>This action cannot be completed at this time.</string>"),
        )
        .unwrap();
        let error = check_error(&res).unwrap_err();
        assert!(matches!(&error, Error::AuthSrpWithMessage(-80045, m) if m.contains("cannot be completed")));
        assert_eq!(
            error.to_string(),
            "Authentication SRP error -80045: This action cannot be completed at this time."
        );
    }

    #[test]
    fn status_without_code_is_not_an_error() {
        let res = parse_gsa_text(200, &plist_of("<key>hsc</key><integer>200</integer>")).unwrap();
        assert!(check_error(&res).is_ok());
        // no panic when the message is missing
        let res = parse_gsa_text(200, &plist_of("<key>ec</key><integer>-1</integer>")).unwrap();
        assert!(matches!(check_error(&res), Err(Error::AuthSrpWithMessage(-1, m)) if m.is_empty()));
    }

    #[test]
    fn hsc_434_asks_for_a_resync_with_the_sim() {
        let xml = plist_of(
            "<key>hsc</key><integer>434</integer><key>X-Apple-I-MD-DATA</key><string>AAAABBGFET7Nxs5itiAQRCfpaNWSpTdMIEYMyWrRJwE=</string><key>ec</key><integer>-22421</integer><key>em</key><string>This action could not be completed. Try again.</string>",
        );
        let res = parse_gsa_text(200, &xml).unwrap();
        assert_eq!(status_hsc(&res), Some(434));
        assert_eq!(resync_sim(&res).as_deref(), Some("AAAABBGFET7Nxs5itiAQRCfpaNWSpTdMIEYMyWrRJwE="));
        // the same answer is still an error for check_error (second 434 after a resync)
        assert!(matches!(check_error(&res), Err(Error::AuthSrpWithMessage(-22421, _))));
    }

    #[test]
    fn sim_given_as_data_is_base64_encoded() {
        let xml = plist_of("<key>hsc</key><integer>434</integer><key>X-Apple-I-MD-DATA</key><data>AAECAw==</data>");
        let res = parse_gsa_text(200, &xml).unwrap();
        assert_eq!(resync_sim(&res).as_deref(), Some("AAECAw=="));
    }

    #[test]
    fn other_statuses_do_not_ask_for_a_resync() {
        let res = parse_gsa_text(200, &plist_of("<key>hsc</key><integer>409</integer>")).unwrap();
        assert_eq!(resync_sim(&res), None);
        assert_eq!(status_hsc(&res), Some(409));
        // 434 without data: nothing to synchronize with
        let res = parse_gsa_text(200, &plist_of("<key>hsc</key><integer>434</integer>")).unwrap();
        assert_eq!(resync_sim(&res), None);
        let res = parse_gsa_text(200, &plist_of("<key>ec</key><integer>0</integer>")).unwrap();
        assert_eq!(status_hsc(&res), None);
    }
}
