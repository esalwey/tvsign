//! The whole sign-in (`Account::login_with_anisette`) against local imitations of GrandSlam (SRP login,
//! two-factor endpoints) and of an anisette server: no Apple, no Internet, no account.
//!
//! What they pin down: the headers every request carries, the synchronization (hsc 434) and its use of
//! the new anisette data, the two-factor decisions (code already pushed, SMS refused), the retries of a
//! shed request and the absence of any retry of a refusal like -80045.

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

use aes::cipher::{BlockModeEncrypt, KeyIvInit, block_padding::Pkcs7};
use hmac::{Hmac, KeyInit, Mac};
use omnisette::AnisetteConfiguration;
use plist::{Dictionary, Value};
use sha2::{Digest, Sha256};
use srp::groups::G2048;

use crate::Error;
use crate::auth::anisette_data::AnisetteData;
use crate::auth::{Account, TEST_GSA_HOST, TwoFactorAction, TwoFactorMethod, TwoFactorRequest};

const WINDOWS: &str = "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>";
const USER_AGENT: &str = "akd/1.0 CFNetwork/808.1.4";
const SIM: &str = "AAAABBGFET7Nxs5itiAQRCfpaNWSpTdMIEYMyWrRJwE=";
const BEFORE_SYNC: &str = "otp-before-sync";
const AFTER_SYNC: &str = "otp-after-sync";
const PASSWORD: &str = "correct horse battery staple";

// ---------------------------------------------------------------- a small HTTP server

#[derive(Clone, Debug)]
struct Req {
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: Vec<u8>,
}

struct Resp {
    status: u16,
    content_type: &'static str,
    body: Vec<u8>,
}

impl Resp {
    fn status(status: u16) -> Resp {
        Resp {
            status,
            content_type: "text/plain",
            body: Vec::new(),
        }
    }

    fn html(status: u16, title: &str) -> Resp {
        Resp {
            status,
            content_type: "text/html",
            body: format!("<html><head><title>{title}</title></head><body>{title}</body></html>").into_bytes(),
        }
    }

    fn json(status: u16, body: &str) -> Resp {
        Resp {
            status,
            content_type: "application/json",
            body: body.as_bytes().to_vec(),
        }
    }

    fn plist(dict: Dictionary) -> Resp {
        let mut body = Vec::new();
        plist::to_writer_xml(&mut body, &Value::Dictionary(dict)).unwrap();
        Resp {
            status: 200,
            content_type: "text/x-xml-plist",
            body,
        }
    }
}

fn serve(handler: impl Fn(&Req) -> Resp + Send + Sync + 'static) -> (String, Arc<Mutex<Vec<Req>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let handler = Arc::new(handler);
    let log = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let handler = handler.clone();
            let log = log.clone();
            std::thread::spawn(move || {
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut request_line = String::new();
                if reader.read_line(&mut request_line).unwrap_or(0) == 0 {
                    return;
                }
                let mut parts = request_line.split_whitespace();
                let method = parts.next().unwrap_or("").to_string();
                let path = parts.next().unwrap_or("").split('?').next().unwrap_or("").to_string();
                let mut headers = HashMap::new();
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some((name, value)) = line.split_once(':') {
                        headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
                    }
                }
                let length = headers
                    .get("content-length")
                    .and_then(|v| v.parse::<usize>().ok())
                    .unwrap_or(0);
                let mut body = vec![0u8; length];
                let _ = reader.read_exact(&mut body);
                let req = Req {
                    method,
                    path,
                    headers,
                    body,
                };
                log.lock().unwrap().push(req.clone());
                let resp = handler(&req);
                let _ = stream.write_all(
                    format!(
                        "HTTP/1.1 {} X\r\nContent-Type: {}\r\nContent-Length: {}\r\nRetry-After: 0\r\nConnection: close\r\n\r\n",
                        resp.status,
                        resp.content_type,
                        resp.body.len()
                    )
                    .as_bytes(),
                );
                let _ = stream.write_all(&resp.body);
            });
        }
    });
    (url, seen)
}

// ---------------------------------------------------------------- the anisette server

fn anisette_server() -> (String, Arc<Mutex<Vec<Req>>>) {
    serve(|req| match req.path.as_str() {
        "/v3/client_info" => Resp::json(
            200,
            &format!(r#"{{"client_info":"{WINDOWS}","user_agent":"{USER_AGENT}"}}"#),
        ),
        "/v3/get_headers" => {
            let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap_or_default();
            let otp = if body.get("sim").is_some() { AFTER_SYNC } else { BEFORE_SYNC };
            Resp::json(
                200,
                &format!(
                    r#"{{"result":"Headers","X-Apple-I-MD-M":"machine","X-Apple-I-MD":"{otp}","X-Apple-I-MD-RINFO":"17106176"}}"#
                ),
            )
        }
        _ => Resp::status(404),
    })
}

/// A configuration whose anisette server is `url` and whose identity is already provisioned.
fn anisette_config(url: &str) -> (AnisetteConfiguration, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!("plume-flow-{}", rand::random::<u64>()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("anisette-state-windows.plist"),
        r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>keychain_identifier</key><data>CQkJCQkJCQkJCQkJCQkJCQ==</data>
<key>adi_pb</key><data>AQID</data></dict></plist>"#,
    )
    .unwrap();
    let config = AnisetteConfiguration::default()
        .set_anisette_url_v3(url.to_string())
        .set_configuration_path(dir.clone());
    (config, dir)
}

// ---------------------------------------------------------------- GrandSlam

#[derive(Clone, Copy, PartialEq)]
enum TwoFa {
    /// Nothing to verify.
    None,
    /// `hsc` 409 and no `au`: the code was already pushed to the trusted devices.
    AlreadyPushed,
    /// `au` = trustedDeviceSecondaryAuth: the client has to ask for the push.
    TrustedDevice,
    /// `au` = secondaryAuth: SMS only.
    SmsOnly,
}

#[derive(Default)]
struct State {
    a_pub: Vec<u8>,
    b: Vec<u8>,
    v: Vec<u8>,
    username: String,
    code_ok: bool,
    prompts_after_code: usize,
}

struct Apple {
    protocol: &'static str,
    salt: Vec<u8>,
    iters: u32,
    two_fa: TwoFa,
    code: &'static str,
    sms_status: u16,
    /// Keep asking for a code even after a good one.
    always_ask: bool,
    /// Answers with 503 to this many requests of the service endpoint first.
    shed: Mutex<usize>,
    refuse_init: Option<(i64, &'static str)>,
    /// The first init (with the unsynchronized one-time password) is told to synchronize.
    needs_sync: bool,
    state: Mutex<State>,
}

impl Apple {
    fn new(two_fa: TwoFa) -> Apple {
        Apple {
            protocol: "s2k",
            salt: vec![7; 16],
            iters: 64,
            two_fa,
            code: "123456",
            sms_status: 403,
            always_ask: false,
            shed: Mutex::new(0),
            refuse_init: None,
            needs_sync: false,
            state: Mutex::new(State::default()),
        }
    }
}

fn dict(entries: Vec<(&str, Value)>) -> Dictionary {
    let mut d = Dictionary::new();
    for (key, value) in entries {
        d.insert(key.to_string(), value);
    }
    d
}

fn status(entries: Vec<(&str, Value)>) -> Value {
    Value::Dictionary(dict(entries))
}

fn response(entries: Vec<(&str, Value)>) -> Resp {
    Resp::plist(dict(vec![("Response", Value::Dictionary(dict(entries)))]))
}

fn string(value: &str) -> Value {
    Value::String(value.to_string())
}

fn int(value: i64) -> Value {
    Value::Integer(value.into())
}

fn hmac(key: &[u8], text: &[u8]) -> Vec<u8> {
    Hmac::<Sha256>::new_from_slice(key)
        .unwrap()
        .chain_update(text)
        .finalize()
        .into_bytes()
        .to_vec()
}

impl Apple {
    fn handle(&self, req: &Req) -> Resp {
        match (req.method.as_str(), req.path.as_str()) {
            ("POST", "/grandslam/GsService2") => self.service(req),
            ("GET", "/auth/verify/trusteddevice") => Resp::status(200),
            ("GET", "/grandslam/GsService2/validate") => self.validate(req),
            ("GET", "/auth") => Resp::json(
                200,
                r#"{"trustedPhoneNumbers":[{"numberWithDialCode":"+33 6 12 34 56 78","lastTwoDigits":"78","pushMode":"sms","id":1}]}"#,
            ),
            ("PUT", "/auth/verify/phone") => {
                if self.sms_status == 403 {
                    Resp::json(403, "")
                } else {
                    Resp::status(self.sms_status)
                }
            }
            _ => Resp::status(404),
        }
    }

    fn validate(&self, req: &Req) -> Resp {
        let mut state = self.state.lock().unwrap();
        if req.headers.get("security-code").map(String::as_str) == Some(self.code) {
            state.code_ok = true;
            Resp::plist(dict(vec![("ec", int(0)), ("em", string(""))]))
        } else {
            Resp::plist(dict(vec![
                ("ec", int(-21669)),
                ("em", string("Incorrect verification code.")),
            ]))
        }
    }

    fn password_bytes(&self) -> Vec<u8> {
        let hashed = Sha256::digest(PASSWORD.as_bytes());
        let pw = if self.protocol == "s2k_fo" {
            hex::encode(hashed).into_bytes()
        } else {
            hashed.to_vec()
        };
        let mut out = [0u8; 32];
        pbkdf2::pbkdf2::<Hmac<Sha256>>(&pw, &self.salt, self.iters, &mut out).unwrap();
        out.to_vec()
    }

    fn service(&self, req: &Req) -> Resp {
        {
            let mut shed = self.shed.lock().unwrap();
            if *shed > 0 {
                *shed -= 1;
                return Resp::html(503, "503 Service Temporarily Unavailable");
            }
        }
        let body: Dictionary = plist::from_bytes(&req.body).unwrap();
        let request = body.get("Request").and_then(Value::as_dictionary).unwrap();
        let cpd = request.get("cpd").and_then(Value::as_dictionary).unwrap();
        let otp = cpd.get("X-Apple-I-MD").and_then(Value::as_string).unwrap_or("");
        let operation = request.get("o").and_then(Value::as_string).unwrap_or("");

        if self.needs_sync && otp == BEFORE_SYNC {
            return response(vec![(
                "Status",
                status(vec![
                    ("hsc", int(434)),
                    ("X-Apple-I-MD-DATA", string(SIM)),
                    ("ec", int(-22421)),
                    ("em", string("This action could not be completed. Try again.")),
                ]),
            )]);
        }
        match operation {
            "init" => self.init(request),
            "complete" => self.complete(request),
            _ => Resp::status(404),
        }
    }

    fn init(&self, request: &Dictionary) -> Resp {
        if let Some((ec, em)) = self.refuse_init {
            return response(vec![("Status", status(vec![("ec", int(ec)), ("em", string(em))]))]);
        }
        let username = request.get("u").and_then(Value::as_string).unwrap().to_string();
        let a_pub = request.get("A2k").and_then(Value::as_data).unwrap().to_vec();
        let v = srp::Client::<G2048, Sha256>::new_with_options(false).compute_verifier(
            username.as_bytes(),
            &self.password_bytes(),
            &self.salt,
        );
        let b: Vec<u8> = (0..32).map(|_| rand::random::<u8>()).collect();
        let b_pub = srp::Server::<G2048, Sha256>::new().compute_public_ephemeral(&b, &v);
        let mut state = self.state.lock().unwrap();
        state.a_pub = a_pub;
        state.b = b;
        state.v = v;
        state.username = username;
        drop(state);
        response(vec![
            ("Status", status(vec![("ec", int(0)), ("em", string(""))])),
            ("s", Value::Data(self.salt.clone())),
            ("B", Value::Data(b_pub)),
            ("i", int(self.iters.into())),
            ("c", string("cookie-1")),
            ("sp", string(self.protocol)),
        ])
    }

    fn complete(&self, request: &Dictionary) -> Resp {
        let mut state = self.state.lock().unwrap();
        let m1 = request.get("M1").and_then(Value::as_data).unwrap();
        let server = srp::Server::<G2048, Sha256>::new();
        let verifier = server
            .process_reply(state.username.as_bytes(), &self.salt, &state.b, &state.v, &state.a_pub)
            .unwrap();
        if verifier.verify_client(m1).is_err() {
            return response(vec![(
                "Status",
                status(vec![
                    ("ec", int(-22406)),
                    ("em", string("Your Apple ID or password was entered incorrectly.")),
                ]),
            )]);
        }

        let spd = dict(vec![
            ("adsid", string("000000-00-00000000-0000-0000-0000-000000000000")),
            ("GsIdmsToken", string("idms-token")),
            ("sk", Value::Data(vec![3; 32])),
            ("c", Value::Data(vec![4; 16])),
            ("fn", string("Test")),
            ("ln", string("User")),
        ]);
        let mut plain = Vec::new();
        plist::to_writer_xml(&mut plain, &Value::Dictionary(spd)).unwrap();
        let key = hmac(verifier.key(), b"extra data key:");
        let iv = hmac(verifier.key(), b"extra data iv:");
        let encrypted = cbc::Encryptor::<aes::Aes256>::new_from_slices(&key, &iv[..16])
            .unwrap()
            .encrypt_padded_vec::<Pkcs7>(&plain);

        let asks = !state.code_ok || self.always_ask;
        if state.code_ok {
            state.prompts_after_code += 1;
        }
        let mut st = vec![("ec", int(0)), ("em", string("")), ("hsc", int(200))];
        if asks {
            match self.two_fa {
                TwoFa::None => {}
                TwoFa::AlreadyPushed => st = vec![("ec", int(0)), ("em", string("")), ("hsc", int(409))],
                TwoFa::TrustedDevice => st.push(("au", string("trustedDeviceSecondaryAuth"))),
                TwoFa::SmsOnly => st.push(("au", string("secondaryAuth"))),
            }
        }
        response(vec![
            ("M2", Value::Data(verifier.proof().to_vec())),
            ("spd", Value::Data(encrypted)),
            ("Status", status(st)),
        ])
    }
}

// ---------------------------------------------------------------- running a sign-in

struct Run {
    result: Result<Account, Error>,
    prompts: Vec<TwoFactorRequest>,
    apple: Vec<Req>,
    anisette: Vec<Req>,
}

impl Run {
    fn count(&self, method: &str, path: &str) -> usize {
        self.apple
            .iter()
            .filter(|r| r.method == method && r.path == path)
            .count()
    }

    fn service_requests(&self) -> Vec<&Req> {
        self.apple
            .iter()
            .filter(|r| r.method == "POST" && r.path == "/grandslam/GsService2")
            .collect()
    }

    fn operations(&self) -> Vec<String> {
        self.service_requests()
            .iter()
            .filter_map(|r| plist::from_bytes::<Dictionary>(&r.body).ok())
            .filter_map(|b| {
                b.get("Request")
                    .and_then(Value::as_dictionary)
                    .and_then(|r| r.get("o"))
                    .and_then(Value::as_string)
                    .map(str::to_string)
            })
            .collect()
    }
}

fn block_on<F: std::future::Future>(future: F) -> F::Output {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(future)
}

fn run_with(apple: Apple, password: &'static str, answers: Vec<TwoFactorAction>) -> Run {
    let apple = Arc::new(apple);
    let handler = apple.clone();
    let (apple_url, apple_seen) = serve(move |req| handler.handle(req));
    let (anisette_url, anisette_seen) = anisette_server();
    let (config, dir) = anisette_config(&anisette_url);

    let mut base_headers = HashMap::new();
    base_headers.insert("X-Mme-Client-Info".to_string(), WINDOWS.to_string());
    base_headers.insert("X-Apple-I-MD".to_string(), BEFORE_SYNC.to_string());
    base_headers.insert("X-Apple-I-MD-M".to_string(), "machine".to_string());
    base_headers.insert("X-Mme-Device-Id".to_string(), "09090909-0909-0909-0909-090909090909".to_string());
    let anisette = AnisetteData {
        base_headers,
        generated_at: SystemTime::now(),
        config,
        user_agent: USER_AGENT.to_string(),
        fresh_identity: false,
    };

    let prompts = Mutex::new(Vec::new());
    let answers = Mutex::new(VecDeque::from(answers));
    TEST_GSA_HOST.with(|host| *host.borrow_mut() = Some(apple_url));
    let result = block_on(Account::login_with_anisette(
        || Ok(("Test.User@Example.com".to_string(), password.to_string())),
        |request: TwoFactorRequest| {
            prompts.lock().unwrap().push(request);
            answers
                .lock()
                .unwrap()
                .pop_front()
                .ok_or_else(|| "no answer scripted".to_string())
        },
        anisette,
    ));
    TEST_GSA_HOST.with(|host| *host.borrow_mut() = None);
    let _ = std::fs::remove_dir_all(dir);

    let run = Run {
        result,
        prompts: prompts.into_inner().unwrap(),
        apple: apple_seen.lock().unwrap().clone(),
        anisette: anisette_seen.lock().unwrap().clone(),
    };
    run
}

fn run(apple: Apple, answers: Vec<TwoFactorAction>) -> Run {
    run_with(apple, PASSWORD, answers)
}

fn code(text: &str) -> TwoFactorAction {
    TwoFactorAction::SubmitCode(text.to_string())
}

// ---------------------------------------------------------------- scenarios

#[test]
fn a_code_already_pushed_is_asked_for_without_a_second_push() {
    let mut apple = Apple::new(TwoFa::AlreadyPushed);
    apple.needs_sync = true;
    let run = run(apple, vec![code("123 456")]);

    run.result.as_ref().unwrap();
    // one prompt, for the trusted-device code, with the SMS still on offer
    assert_eq!(run.prompts.len(), 1);
    assert_eq!(run.prompts[0].method, TwoFactorMethod::Device);
    assert!(!run.prompts[0].sms_refused);
    assert_eq!(run.prompts[0].trusted_phone_numbers.len(), 1);
    // Apple already sent it: no request to send another
    assert_eq!(run.count("GET", "/auth/verify/trusteddevice"), 0);
    // the code reached Apple reduced to its characters
    let validate: Vec<_> = run.apple.iter().filter(|r| r.path.ends_with("/validate")).collect();
    assert_eq!(validate.len(), 1);
    assert_eq!(validate[0].headers["security-code"], "123456");
    // init (told to synchronize), init again, complete; then after the code: init, complete
    assert_eq!(run.operations(), ["init", "init", "complete", "init", "complete"]);
}

#[test]
fn the_synchronization_gives_new_anisette_data_to_every_following_request() {
    let mut apple = Apple::new(TwoFa::None);
    apple.needs_sync = true;
    let run = run(apple, vec![]);
    run.result.as_ref().unwrap();

    // the anisette server was asked once, with Apple's sim and the identity
    let asked: Vec<serde_json::Value> = run
        .anisette
        .iter()
        .filter(|r| r.path == "/v3/get_headers")
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect();
    assert_eq!(asked.len(), 1);
    assert_eq!(asked[0]["sim"], SIM);
    assert!(asked[0]["device_id"].is_string() && asked[0]["md_lu"].is_string());

    // first init with the old password, the retried init and complete with the new one
    let otps: Vec<String> = run
        .service_requests()
        .iter()
        .map(|r| {
            let body: Dictionary = plist::from_bytes(&r.body).unwrap();
            body.get("Request")
                .and_then(Value::as_dictionary)
                .and_then(|r| r.get("cpd"))
                .and_then(Value::as_dictionary)
                .and_then(|c| c.get("X-Apple-I-MD"))
                .and_then(Value::as_string)
                .unwrap()
                .to_string()
        })
        .collect();
    assert_eq!(otps, [BEFORE_SYNC, AFTER_SYNC, AFTER_SYNC]);
}

#[test]
fn every_grandslam_request_carries_the_client_info_and_user_agent_of_the_anisette_server() {
    let run = run(Apple::new(TwoFa::TrustedDevice), vec![code("123456")]);
    run.result.as_ref().unwrap();
    assert!(run.apple.len() >= 6);
    for req in &run.apple {
        assert_eq!(req.headers["x-mme-client-info"], WINDOWS, "{} {}", req.method, req.path);
        assert_eq!(req.headers["user-agent"], USER_AGENT, "{} {}", req.method, req.path);
        assert_eq!(req.headers["connection"], "close");
        assert_eq!(req.headers["x-apple-app-info"], "com.apple.gs.xcode.auth");
        assert!(req.headers.contains_key("x-xcode-version"));
    }
    // 2FA calls carry the anisette data and the identity token of the session
    let push = run.apple.iter().find(|r| r.path == "/auth/verify/trusteddevice").unwrap();
    assert_eq!(push.headers["x-apple-i-md"], BEFORE_SYNC);
    assert!(push.headers.contains_key("x-apple-identity-token"));
    assert!(push.headers.contains_key("x-mme-device-id"));
    // a push was asked for, once
    assert_eq!(run.count("GET", "/auth/verify/trusteddevice"), 1);
}

#[test]
fn sms_refused_goes_back_to_the_device_code_without_the_sms_offer() {
    let run = run(
        Apple::new(TwoFa::TrustedDevice),
        vec![TwoFactorAction::SendSms(1), code("123456")],
    );
    run.result.as_ref().unwrap();
    assert_eq!(run.count("PUT", "/auth/verify/phone"), 1);
    assert_eq!(run.prompts.len(), 2);
    assert!(!run.prompts[0].sms_refused);
    assert!(!run.prompts[0].trusted_phone_numbers.is_empty());
    assert_eq!(run.prompts[1].method, TwoFactorMethod::Device);
    assert!(run.prompts[1].sms_refused);
    assert!(run.prompts[1].trusted_phone_numbers.is_empty());
    // the code of the first push is the one that was used: no second push
    assert_eq!(run.count("GET", "/auth/verify/trusteddevice"), 1);
}

#[test]
fn an_account_with_no_trusted_device_gets_a_clear_message_when_sms_is_refused() {
    let run = run(Apple::new(TwoFa::SmsOnly), vec![]);
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::SmsRefused { status: 403, .. }), "{error}");
    let text = error.to_string();
    assert!(text.contains("SMS 2FA refused by Apple") && text.contains("trusted Apple device"), "{text}");
    assert!(run.prompts.is_empty());
}

#[test]
fn a_shed_request_is_sent_again_and_the_login_goes_on() {
    let apple = Apple::new(TwoFa::None);
    *apple.shed.lock().unwrap() = 2;
    let run = run(apple, vec![]);
    run.result.as_ref().unwrap();
    // two 503, then init and complete
    assert_eq!(run.service_requests().len(), 4);
}

#[test]
fn a_503_that_lasts_is_reported_after_the_retries() {
    let apple = Apple::new(TwoFa::None);
    *apple.shed.lock().unwrap() = 100;
    let run = run(apple, vec![]);
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::GsaHttp { status: 503, .. }), "{error}");
    assert!(error.to_string().contains("503 Service Temporarily Unavailable"));
    assert_eq!(run.service_requests().len(), 4, "one try and three retries, no more");
}

#[test]
fn minus_80045_is_reported_and_never_sent_again() {
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_init = Some((-80045, "This action cannot be completed at this time."));
    let run = run(apple, vec![]);
    let error = run.result.as_ref().unwrap_err();
    assert_eq!(
        error.to_string(),
        "Authentication SRP error -80045: This action cannot be completed at this time."
    );
    assert_eq!(run.service_requests().len(), 1);
}

#[test]
fn the_srp_protocol_apple_selects_is_followed() {
    for protocol in ["s2k", "s2k_fo"] {
        let mut apple = Apple::new(TwoFa::None);
        apple.protocol = protocol;
        let run = run(apple, vec![]);
        assert!(run.result.is_ok(), "{protocol}: {:?}", run.result.err());
    }
}

#[test]
fn a_wrong_password_is_apples_answer() {
    let run = run_with(Apple::new(TwoFa::None), "not the password", vec![]);
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::AuthSrpWithMessage(-22406, _)), "{error}");
}

#[test]
fn a_wrong_code_ends_the_login_with_apples_message() {
    let run = run(Apple::new(TwoFa::AlreadyPushed), vec![code("000000")]);
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::AuthSrpWithMessage(-21669, _)), "{error}");
}

#[test]
fn an_apple_that_keeps_asking_for_codes_ends_the_login() {
    let mut apple = Apple::new(TwoFa::AlreadyPushed);
    apple.always_ask = true;
    let run = run(apple, vec![code("123456"), code("123456"), code("123456"), code("123456"), code("123456")]);
    let error = run.result.as_ref().unwrap_err();
    assert!(error.to_string().contains("keeps asking"), "{error}");
    assert_eq!(run.prompts.len(), 4);
}
