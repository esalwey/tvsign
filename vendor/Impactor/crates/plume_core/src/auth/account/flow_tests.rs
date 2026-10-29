//! The whole sign-in (`Account::login_with_anisette`) against local imitations of GrandSlam (SRP login,
//! two-factor endpoints) and of an anisette server: no Apple, no Internet, no account.
//!
//! What they pin down: the headers every request carries, the synchronization (hsc 434) and its use of
//! the new anisette data, the two-factor decisions (code already pushed, SMS refused), the retries of a
//! shed request and the absence of any retry of a refusal like -80045, the identity that Apple does not
//! know (-45061: provisioned again through the imitation of the anisette server and of Apple's
//! provisioning service, then synchronized in its turn), the switch to the next anisette server when
//! Apple refuses the data of the first (-80045, an HTML 503, -45061 or -22421 that stay), the memory of
//! the server Apple accepted, the client info and user agent whatever the servers announce (or the
//! override that replaces them), and the developer portal's view of the same data.

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::SystemTime;

use aes::cipher::{BlockModeEncrypt, KeyIvInit, block_padding::Pkcs7};
use hmac::{Hmac, KeyInit, Mac};
use omnisette::{AnisetteConfiguration, ClientOverrides};
use plist::{Dictionary, Value};
use sha2::{Digest, Sha256};
use srp::groups::G2048;

use crate::Error;
use crate::auth::anisette_data::AnisetteData;
use crate::auth::{Account, TEST_GSA_HOST, TwoFactorAction, TwoFactorMethod, TwoFactorRequest};

/// What the anisette servers announce today (October 2026): a Mac.
const CLIENT_INFO: &str = "<MacBookPro13,3> <Mac OS X;10.12.6;16G29> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";
const USER_AGENT: &str = "akd/1.0 CFNetwork/811.5.4 Darwin/16.7.0";
/// What they announced until 2026-10-22: Windows. And the Android of the servers of September. Nothing
/// in the sign-in depends on which: the pair of the server goes to Apple, and Apple's answer decides.
const WINDOWS: &str = "<PC> <Windows;6.2(0,0);9200> <com.apple.AuthKit/1 (com.apple.iTunes/12.12.4.1)>";
const WINDOWS_USER_AGENT: &str = "akd/1.0 CFNetwork/808.1.4";
const ANDROID: &str = "<Pixel 7> <Android;13;TQ3A.230901.001> <com.apple.AuthKit/1 (com.apple.akd/1.0)>";
const ANDROID_USER_AGENT: &str = "akd/1.0 CFNetwork/978.0.7";
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
    let (url, seen, _) = serve_with_provisioning(handler);
    (url, seen)
}

/// The provisioning socket of an anisette server (`/v3/provisioning_session`, a websocket) as the
/// anisette-v3 servers speak it: the identifier is asked for and recorded, then Apple's start and finish
/// data are asked for and handed over, and the provisioning succeeds with a provisioning blob.
fn provisioning_socket(mut stream: TcpStream, key: &str, identifiers: &Mutex<Vec<String>>) {
    use base64::{Engine, engine::general_purpose::STANDARD};

    let accept = STANDARD.encode(<sha1::Sha1 as sha1::Digest>::digest(
        format!("{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11").as_bytes(),
    ));
    let _ = write!(
        stream,
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    );

    // server -> client: an unmasked text frame
    fn send(stream: &mut TcpStream, text: &str) {
        let mut frame = vec![0x81];
        if text.len() < 126 {
            frame.push(text.len() as u8);
        } else {
            frame.push(126);
            frame.extend((text.len() as u16).to_be_bytes());
        }
        frame.extend(text.as_bytes());
        stream.write_all(&frame).unwrap();
    }
    // client -> server: a masked text frame
    fn receive(stream: &mut TcpStream) -> serde_json::Value {
        let mut head = [0u8; 2];
        stream.read_exact(&mut head).unwrap();
        assert_eq!(head[0] & 0x0f, 1, "a text frame was expected");
        let mut length = usize::from(head[1] & 0x7f);
        if length == 126 {
            let mut extended = [0u8; 2];
            stream.read_exact(&mut extended).unwrap();
            length = usize::from(u16::from_be_bytes(extended));
        }
        let mut mask = [0u8; 4];
        stream.read_exact(&mut mask).unwrap();
        let mut payload = vec![0u8; length];
        stream.read_exact(&mut payload).unwrap();
        for (i, byte) in payload.iter_mut().enumerate() {
            *byte ^= mask[i % 4];
        }
        serde_json::from_slice(&payload).unwrap()
    }

    send(&mut stream, r#"{"result":"GiveIdentifier"}"#);
    let reply = receive(&mut stream);
    identifiers.lock().unwrap().push(reply["identifier"].as_str().unwrap().to_string());
    send(&mut stream, r#"{"result":"GiveStartProvisioningData"}"#);
    assert_eq!(receive(&mut stream)["spim"], "c3BpbQ==");
    send(&mut stream, r#"{"result":"GiveEndProvisioningData","cpim":"Y3BpbQ=="}"#);
    let end = receive(&mut stream);
    assert_eq!((end["ptm"].as_str(), end["tk"].as_str()), (Some("cHRt"), Some("dGs=")));
    send(&mut stream, r#"{"result":"ProvisioningSuccess","adi_pb":"YWRpLXByb3Zpc2lvbmVk"}"#);
}

/// [`serve`], and a websocket upgrade is answered by [`provisioning_socket`] (the identifiers it was
/// given are the third result).
fn serve_with_provisioning(
    handler: impl Fn(&Req) -> Resp + Send + Sync + 'static,
) -> (String, Arc<Mutex<Vec<Req>>>, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let identifiers = Arc::new(Mutex::new(Vec::new()));
    let handler = Arc::new(handler);
    let log = seen.clone();
    let sockets = identifiers.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let handler = handler.clone();
            let log = log.clone();
            let sockets = sockets.clone();
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
                if let Some(key) = headers.get("sec-websocket-key") {
                    provisioning_socket(stream, key, &sockets);
                    return;
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
    (url, seen, identifiers)
}

// ---------------------------------------------------------------- the anisette server

/// The one-time password a server whose plain one is `plain` gives once Apple's synchronization data
/// came with the request.
fn synced_otp(plain: &str) -> String {
    if plain == BEFORE_SYNC {
        AFTER_SYNC.to_string()
    } else {
        format!("{plain}-synced")
    }
}

/// An anisette server that announces `client_info` and `user_agent`, whose plain `get_headers` give
/// the one-time password `plain_otp` (and its synchronized one when Apple's synchronization data come
/// with the request). It also speaks the provisioning socket; the third result is the identifiers
/// that were provisioned through it.
fn anisette_server_announcing(
    plain_otp: &'static str,
    client_info: &'static str,
    user_agent: &'static str,
) -> (String, Arc<Mutex<Vec<Req>>>, Arc<Mutex<Vec<String>>>) {
    anisette_server(plain_otp, client_info, user_agent, false)
}

/// [`anisette_server_announcing`]; with `fails_to_synchronize` the server answers an error to the
/// request that carries Apple's synchronization data (but gives plain data without them).
fn anisette_server(
    plain_otp: &'static str,
    client_info: &'static str,
    user_agent: &'static str,
    fails_to_synchronize: bool,
) -> (String, Arc<Mutex<Vec<Req>>>, Arc<Mutex<Vec<String>>>) {
    let synced = synced_otp(plain_otp);
    serve_with_provisioning(move |req| match req.path.as_str() {
        "/v3/client_info" => Resp::json(
            200,
            &format!(r#"{{"client_info":"{client_info}","user_agent":"{user_agent}"}}"#),
        ),
        "/v3/get_headers" => {
            let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap_or_default();
            if fails_to_synchronize && body.get("sim").is_some() {
                return Resp::json(500, r#"{"result":"GetHeadersError","message":"ADI error -1: cannot synchronize"}"#);
            }
            let otp = if body.get("sim").is_some() { synced.as_str() } else { plain_otp };
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

/// An anisette server announcing a Mac, as they all do now.
fn anisette_server_giving(
    plain_otp: &'static str,
) -> (String, Arc<Mutex<Vec<Req>>>, Arc<Mutex<Vec<String>>>) {
    anisette_server_announcing(plain_otp, CLIENT_INFO, USER_AGENT)
}

const STATE_XML: &str = r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>keychain_identifier</key><data>CQkJCQkJCQkJCQkJCQkJCQ==</data>
<key>adi_pb</key><data>AQID</data></dict></plist>"#;

/// A configuration whose anisette servers are `urls`, in this order, and whose identity with each of
/// them is already provisioned (one file per server, as plumesign keeps them).
fn anisette_config(urls: &[&str]) -> (AnisetteConfiguration, PathBuf) {
    let dir = std::env::temp_dir().join(format!("plume-flow-{}", rand::random::<u64>()));
    for url in urls {
        let path = omnisette::remote_anisette_v3::state_path(&dir, url);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, STATE_XML).unwrap();
    }
    let config = AnisetteConfiguration::default()
        .set_servers_v3(urls.iter().map(|url| url.to_string()).collect())
        .set_configuration_path(dir.clone());
    (config, dir)
}

/// The device identifier the identity stored in `dir` for `server` has now (empty when there is none).
fn stored_identifier(dir: &std::path::Path, server: &str) -> Vec<u8> {
    plist::Value::from_file(omnisette::remote_anisette_v3::state_path(dir, server))
        .ok()
        .as_ref()
        .and_then(Value::as_dictionary)
        .and_then(|d| d.get("keychain_identifier"))
        .and_then(Value::as_data)
        .map(<[u8]>::to_vec)
        .unwrap_or_default()
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
    /// Requests that carry this one-time password get -45061: Apple does not know that provisioning.
    stale_otp: Option<&'static str>,
    /// Requests that carry this one-time password get the HTML 503 page: Apple refuses the data of
    /// the server that produced it.
    refuse_otp: Option<&'static str>,
    /// Requests that carry one of these one-time passwords get this error code and message (-80045:
    /// Apple refuses the data of the server that produced it).
    refuse_otp_with: Vec<(&'static str, i64, &'static str)>,
    /// Requests that carry one of these one-time passwords are told to synchronize (hsc 434), however
    /// many times they have been.
    ask_sync_otps: Vec<&'static str>,
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
            stale_otp: None,
            refuse_otp: None,
            refuse_otp_with: Vec::new(),
            ask_sync_otps: Vec::new(),
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
            // Apple's provisioning service, which the anisette identity goes through
            ("GET", "/grandslam/GsService2/lookup") => {
                let host = req.headers.get("host").cloned().unwrap_or_default();
                Resp::plist(dict(vec![(
                    "urls",
                    status(vec![
                        ("midStartProvisioning", string(&format!("http://{host}/mid/start"))),
                        ("midFinishProvisioning", string(&format!("http://{host}/mid/finish"))),
                    ]),
                )]))
            }
            ("POST", "/mid/start") => response(vec![("spim", string("c3BpbQ=="))]),
            ("POST", "/mid/finish") => response(vec![("ptm", string("cHRt")), ("tk", string("dGs="))]),
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

        if self.refuse_otp == Some(otp) {
            return Resp::html(503, "503 Service Temporarily Unavailable");
        }
        if self.stale_otp == Some(otp) {
            return response(vec![(
                "Status",
                status(vec![("ec", int(-45061)), ("em", string("The provisioning is not valid."))]),
            )]);
        }
        if let Some((_, ec, em)) = self.refuse_otp_with.iter().find(|(refused, _, _)| *refused == otp) {
            return response(vec![("Status", status(vec![("ec", int(*ec)), ("em", string(em))]))]);
        }
        if (self.needs_sync && otp == BEFORE_SYNC) || self.ask_sync_otps.contains(&otp) {
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
    /// URL of the first anisette server.
    anisette_url: String,
    /// What the first anisette server received.
    anisette: Vec<Req>,
    /// What the other anisette servers received (the ones behind the first), in order.
    other_anisette: Vec<Vec<Req>>,
    /// The identifiers (base64) that were provisioned through the anisette servers' sockets.
    provisioned: Vec<String>,
    /// The device identifier of the stored anisette identity with the first server when the sign-in
    /// ended.
    identifier: Vec<u8>,
    /// The URLs of all the anisette servers of the configuration, in order (dead ones included).
    urls: Vec<String>,
    /// The server the configuration folder remembers as the one Apple accepted, when the sign-in
    /// ended.
    remembered: Option<String>,
}

impl Run {
    fn count(&self, method: &str, path: &str) -> usize {
        self.apple
            .iter()
            .filter(|r| r.method == method && r.path == path)
            .count()
    }

    fn anisette_url(&self) -> String {
        self.anisette_url.clone()
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

/// A sign-in to run against the imitations.
struct Scenario {
    apple: Apple,
    password: &'static str,
    answers: Vec<TwoFactorAction>,
    /// The one-time password that each anisette server gives to a plain request, in the order the
    /// servers are configured.
    servers: Vec<&'static str>,
    /// The one-time password of the anisette data the sign-in starts with.
    initial_otp: &'static str,
    /// The client info and user agent the anisette servers announce.
    announces: (&'static str, &'static str),
    /// Servers that are down (nothing listens), configured before the others.
    dead_servers: usize,
    /// The client info and user agent to send instead of what the servers announce.
    overrides: ClientOverrides,
    /// Start from the anisette servers themselves (`Account::login`, as plumesign does) instead of
    /// from data made by the test.
    from_servers: bool,
    /// Indexes of the servers that fail when they are given Apple's synchronization data.
    failing_sync: Vec<usize>,
    /// Apple accepted the data of the first server in an earlier sign-in (`last-server.json`).
    trusted_first: bool,
}

impl Scenario {
    fn new(apple: Apple) -> Scenario {
        Scenario {
            apple,
            password: PASSWORD,
            answers: Vec::new(),
            servers: vec![BEFORE_SYNC],
            initial_otp: BEFORE_SYNC,
            announces: (CLIENT_INFO, USER_AGENT),
            dead_servers: 0,
            overrides: ClientOverrides::default(),
            from_servers: false,
            failing_sync: Vec::new(),
            trusted_first: false,
        }
    }
}

/// The URL of a server that is down: a port nobody listens to.
fn dead_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    format!("http://{}", listener.local_addr().unwrap())
}

fn run_scenario(scenario: Scenario) -> Run {
    let apple = Arc::new(scenario.apple);
    let handler = apple.clone();
    let (apple_url, apple_seen) = serve(move |req| handler.handle(req));
    let (client_info, user_agent) = scenario.announces;
    let servers: Vec<_> = scenario
        .servers
        .iter()
        .enumerate()
        .map(|(i, otp)| anisette_server(otp, client_info, user_agent, scenario.failing_sync.contains(&i)))
        .collect();
    let dead: Vec<String> = (0..scenario.dead_servers).map(|_| dead_url()).collect();
    let urls: Vec<String> = dead.iter().cloned().chain(servers.iter().map(|server| server.0.clone())).collect();
    let (config, dir) = anisette_config(&urls.iter().map(String::as_str).collect::<Vec<_>>());
    let config = config.set_client_overrides(scenario.overrides.clone());
    if scenario.trusted_first {
        omnisette::remember_server(&dir, config.configured_servers_v3(), &urls[0]);
    }

    let mut base_headers = HashMap::new();
    base_headers.insert("X-Mme-Client-Info".to_string(), client_info.to_string());
    base_headers.insert("X-Apple-I-MD".to_string(), scenario.initial_otp.to_string());
    base_headers.insert("X-Apple-I-MD-M".to_string(), "machine".to_string());
    base_headers.insert("X-Mme-Device-Id".to_string(), "09090909-0909-0909-0909-090909090909".to_string());
    let anisette = AnisetteData {
        base_headers,
        generated_at: SystemTime::now(),
        config: config.clone(),
        user_agent: user_agent.to_string(),
        fresh_identity: false,
        refresh_failed_at: None,
        server: Some(servers[0].0.clone()),
    };

    let prompts = Mutex::new(Vec::new());
    let answers = Mutex::new(VecDeque::from(scenario.answers));
    let password = scenario.password;
    let ask_credentials = || Ok(("Test.User@Example.com".to_string(), password.to_string()));
    let ask_code = |request: TwoFactorRequest| {
        prompts.lock().unwrap().push(request);
        answers
            .lock()
            .unwrap()
            .pop_front()
            .ok_or_else(|| "no answer scripted".to_string())
    };
    // the sign-in runs on this thread: Apple, and the provisioning of a new identity, are local
    TEST_GSA_HOST.with(|host| *host.borrow_mut() = Some(apple_url.clone()));
    omnisette::remote_anisette_v3::set_lookup_url_for_tests(Some(format!(
        "{apple_url}/grandslam/GsService2/lookup"
    )));
    let result = if scenario.from_servers {
        block_on(Account::login(ask_credentials, ask_code, config))
    } else {
        block_on(Account::login_with_anisette(ask_credentials, ask_code, anisette))
    };
    omnisette::remote_anisette_v3::set_lookup_url_for_tests(None);
    TEST_GSA_HOST.with(|host| *host.borrow_mut() = None);
    let identifier = stored_identifier(&dir, &servers[0].0);
    let remembered = omnisette::remote_anisette_v3::remembered_server(&dir, &urls);
    let _ = std::fs::remove_dir_all(dir);

    let received = |server: &(String, Arc<Mutex<Vec<Req>>>, Arc<Mutex<Vec<String>>>)| server.1.lock().unwrap().clone();
    Run {
        result,
        prompts: prompts.into_inner().unwrap(),
        apple: apple_seen.lock().unwrap().clone(),
        anisette_url: servers[0].0.clone(),
        anisette: received(&servers[0]),
        other_anisette: servers[1..].iter().map(received).collect(),
        provisioned: servers.iter().flat_map(|server| server.2.lock().unwrap().clone()).collect(),
        identifier,
        urls,
        remembered,
    }
}

fn run_with(apple: Apple, password: &'static str, answers: Vec<TwoFactorAction>) -> Run {
    run_scenario(Scenario {
        password,
        answers,
        ..Scenario::new(apple)
    })
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
        assert_eq!(req.headers["x-mme-client-info"], CLIENT_INFO, "{} {}", req.method, req.path);
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
    *apple.shed.lock().unwrap() = 1;
    let run = run(apple, vec![]);
    run.result.as_ref().unwrap();
    // one 503, then init and complete
    assert_eq!(run.service_requests().len(), 3);
}

#[test]
fn a_503_that_lasts_is_reported_after_the_retry() {
    let apple = Apple::new(TwoFa::None);
    *apple.shed.lock().unwrap() = 100;
    let run = run(apple, vec![]);
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::GsaHttp { status: 503, .. }), "{error}");
    assert!(error.to_string().contains("503 Service Temporarily Unavailable"));
    // a refusal gives the same page every time: one try and one retry, and with no other anisette
    // server to take the data from, nothing more
    assert_eq!(run.service_requests().len(), 2);
    assert!(run.other_anisette.is_empty());
}

/// The one-time password of every request to the service endpoint, in order.
fn otps(run: &Run) -> Vec<String> {
    run.service_requests()
        .iter()
        .map(|r| {
            let body: Dictionary = plist::from_bytes(&r.body).unwrap();
            let cpd = body.get("Request").and_then(Value::as_dictionary).and_then(|r| r.get("cpd"));
            cpd.and_then(Value::as_dictionary)
                .and_then(|c| c.get("X-Apple-I-MD"))
                .and_then(Value::as_string)
                .unwrap()
                .to_string()
        })
        .collect()
}

#[test]
fn apple_refusing_the_data_of_the_first_anisette_server_gets_the_data_of_the_other() {
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_otp = Some("otp-primary");
    let run = run_scenario(Scenario {
        servers: vec!["otp-primary", BEFORE_SYNC],
        initial_otp: "otp-primary",
        ..Scenario::new(apple)
    });
    let account = run.result.as_ref().unwrap();

    // the refused init and its retry, then init and complete with the data of the other server
    assert_eq!(run.operations(), ["init", "init", "init", "complete"]);
    assert_eq!(otps(&run), ["otp-primary", "otp-primary", BEFORE_SYNC, BEFORE_SYNC]);
    // the first server was not asked for new data, the second was, once
    assert!(run.anisette.iter().all(|r| r.path != "/v3/get_headers"));
    assert_eq!(run.other_anisette[0].iter().filter(|r| r.path == "/v3/get_headers").count(), 1);
    // and the rest of the sign-in (the later refreshes of the data) keeps to the server that works
    let preferred = block_on(async { account.anisette.lock().await.config.preferred_url_v3().cloned() });
    assert!(preferred.is_some_and(|url| url != run.anisette_url()));
}

#[test]
fn apple_refusing_the_data_of_both_anisette_servers_is_reported_without_looping() {
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_otp = Some("otp-refused");
    let run = run_scenario(Scenario {
        servers: vec!["otp-refused", "otp-refused"],
        initial_otp: "otp-refused",
        ..Scenario::new(apple)
    });
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::GsaHttp { status: 503, .. }), "{error}");
    // two requests with the data of each server, and then it is the end
    assert_eq!(run.service_requests().len(), 4);
    assert_eq!(run.other_anisette[0].iter().filter(|r| r.path == "/v3/get_headers").count(), 1);
}

#[test]
fn a_provisioning_apple_does_not_know_is_made_again_and_the_new_identity_is_synchronized() {
    let mut apple = Apple::new(TwoFa::None);
    apple.stale_otp = Some("otp-stale");
    apple.needs_sync = true;
    let run = run_scenario(Scenario {
        initial_otp: "otp-stale",
        ..Scenario::new(apple)
    });
    run.result.as_ref().unwrap();

    // -45061; the new identity, which Apple has never seen, is told to synchronize (434) and used to
    // be answered with -22421 here; then init and complete with the synchronized data
    assert_eq!(run.operations(), ["init", "init", "init", "complete"]);
    assert_eq!(otps(&run), ["otp-stale", BEFORE_SYNC, AFTER_SYNC, AFTER_SYNC]);
    // a new identity went through the provisioning socket, with Apple's provisioning service
    assert_eq!(run.provisioned.len(), 1);
    assert_ne!(run.identifier, [9u8; 16].to_vec());
    {
        use base64::{Engine, engine::general_purpose::STANDARD};
        assert_eq!(run.provisioned[0], STANDARD.encode(&run.identifier));
    }
    assert_eq!(run.count("GET", "/grandslam/GsService2/lookup"), 1);
    assert_eq!(run.count("POST", "/mid/start"), 1);
    assert_eq!(run.count("POST", "/mid/finish"), 1);
    // the anisette server was asked for the headers of the new identity, then for the synchronized ones
    let asked: Vec<serde_json::Value> = run
        .anisette
        .iter()
        .filter(|r| r.path == "/v3/get_headers")
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect();
    assert_eq!(asked.len(), 2);
    assert!(asked[0].get("sim").is_none());
    assert_eq!(asked[1]["sim"], SIM);
    assert_eq!(asked[0]["identifier"], asked[1]["identifier"]);
}

#[test]
fn a_provisioning_that_stays_invalid_is_not_made_again_for_ever() {
    // whatever the identity, Apple says -45061: one new identity, and the answer is the error
    let mut apple = Apple::new(TwoFa::None);
    apple.stale_otp = Some(BEFORE_SYNC);
    let run = run_scenario(Scenario::new(apple));
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::AuthSrpWithMessage(-45061, _)), "{error}");
    assert_eq!(run.operations(), ["init", "init"]);
    assert_eq!(run.provisioned.len(), 1);
}

#[test]
fn minus_80045_with_a_single_server_is_reported_and_never_sent_again() {
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_init = Some((-80045, "This action cannot be completed at this time."));
    let run = run(apple, vec![]);
    let error = run.result.as_ref().unwrap_err();
    assert_eq!(
        error.to_string(),
        "Authentication SRP error -80045: This action cannot be completed at this time."
    );
    // no other server to take data from: the refusal is the answer, asked once
    assert_eq!(run.service_requests().len(), 1);
    assert_eq!(run.remembered, None, "Apple did not accept these data");
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
fn a_wrong_code_is_asked_again() {
    let run = run(Apple::new(TwoFa::AlreadyPushed), vec![code("000000"), code("123 456")]);
    run.result.as_ref().unwrap();
    assert_eq!(run.prompts.len(), 2);
}

#[test]
fn wrong_codes_end_the_login_with_apples_message_after_three_retries() {
    let run = run(
        Apple::new(TwoFa::AlreadyPushed),
        vec![code("000000"), code("000001"), code("000002"), code("000003")],
    );
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::AuthSrpWithMessage(-21669, _)), "{error}");
    assert_eq!(run.prompts.len(), 4);
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

// ---------------------------------------------------------------- several anisette servers

const REFUSED_80045: &str = "This action cannot be completed at this time.";

#[test]
fn minus_80045_for_the_data_of_a_server_gets_the_data_of_the_next_one() {
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_otp_with = vec![("otp-first", -80045, REFUSED_80045)];
    let run = run_scenario(Scenario {
        servers: vec!["otp-first", BEFORE_SYNC],
        initial_otp: "otp-first",
        ..Scenario::new(apple)
    });
    run.result.as_ref().unwrap();

    // the refusal is not repeated to the same server: init with the second server's data, then complete
    assert_eq!(run.operations(), ["init", "init", "complete"]);
    assert_eq!(otps(&run), ["otp-first", BEFORE_SYNC, BEFORE_SYNC]);
    // the first server was not asked for new data, the second was, once
    assert!(run.anisette.iter().all(|r| r.path != "/v3/get_headers"));
    assert_eq!(run.other_anisette[0].iter().filter(|r| r.path == "/v3/get_headers").count(), 1);
    // the one Apple accepted is remembered, for the next process
    assert_eq!(run.remembered.as_deref(), Some(run.urls[1].as_str()));
}

#[test]
fn minus_80045_from_every_server_is_reported_after_one_other_server() {
    // -80045 is as likely the account or the connection as the data: a tour of every server would be a new
    // device identifier, a provisioning and a synchronization each, for one Apple ID, within seconds
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_otp_with = vec![
        ("otp-a", -80045, REFUSED_80045),
        ("otp-b", -80045, REFUSED_80045),
        ("otp-c", -80045, REFUSED_80045),
    ];
    let run = run_scenario(Scenario {
        servers: vec!["otp-a", "otp-b", "otp-c"],
        initial_otp: "otp-a",
        ..Scenario::new(apple)
    });
    let error = run.result.as_ref().unwrap_err();
    assert_eq!(error.to_string(), format!("Authentication SRP error -80045: {REFUSED_80045}"));
    assert_eq!(otps(&run), ["otp-a", "otp-b"], "one other server, none repeated, the third never asked");
    assert!(run.other_anisette[1].iter().all(|r| r.path != "/v3/get_headers"));
    assert_eq!(run.remembered, None, "no server was accepted");
}

#[test]
fn minus_80045_from_the_server_apple_accepted_last_time_is_the_answer_at_once() {
    // that server's data were good a moment ago: the account or the connection is what Apple refuses, and
    // no other server's data can change that
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_otp_with = vec![("otp-first", -80045, REFUSED_80045)];
    let run = run_scenario(Scenario {
        servers: vec!["otp-first", BEFORE_SYNC],
        initial_otp: "otp-first",
        trusted_first: true,
        ..Scenario::new(apple)
    });
    let error = run.result.as_ref().unwrap_err();
    assert_eq!(error.to_string(), format!("Authentication SRP error -80045: {REFUSED_80045}"));
    assert_eq!(otps(&run), ["otp-first"]);
    assert!(run.other_anisette[0].iter().all(|r| r.path != "/v3/get_headers"));
    assert!(run.provisioned.is_empty());
}

#[test]
fn the_servers_are_tried_in_their_order_until_apple_accepts_one() {
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_otp_with = vec![("otp-a", -80045, REFUSED_80045), ("otp-b", -80009, "MID is invalid.")];
    apple.refuse_otp = Some("otp-c");
    let run = run_scenario(Scenario {
        servers: vec!["otp-a", "otp-b", "otp-c", BEFORE_SYNC, "otp-never-asked"],
        initial_otp: "otp-a",
        ..Scenario::new(apple)
    });
    run.result.as_ref().unwrap();
    // -80045, -80009, the HTML 503 (tried twice), then the data Apple takes; the last one is not asked
    assert_eq!(otps(&run), ["otp-a", "otp-b", "otp-c", "otp-c", BEFORE_SYNC, BEFORE_SYNC]);
    assert!(run.other_anisette[3].iter().all(|r| r.path != "/v3/get_headers"));
    assert_eq!(run.remembered.as_deref(), Some(run.urls[3].as_str()));
}

#[test]
fn a_synchronization_that_does_not_take_gives_way_to_the_next_server() {
    // minus 22421 again after the synchronization: the data of this server are not good
    let mut apple = Apple::new(TwoFa::None);
    apple.ask_sync_otps = vec!["otp-a", "otp-a-synced"];
    let run = run_scenario(Scenario {
        servers: vec!["otp-a", BEFORE_SYNC],
        initial_otp: "otp-a",
        ..Scenario::new(apple)
    });
    run.result.as_ref().unwrap();
    assert_eq!(run.operations(), ["init", "init", "init", "complete"]);
    assert_eq!(otps(&run), ["otp-a", "otp-a-synced", BEFORE_SYNC, BEFORE_SYNC]);
    // the synchronization went to the server whose data Apple asked for it, and to that one only
    let sims = |requests: &[Req]| requests.iter().filter(|r| r.path == "/v3/get_headers").count();
    assert_eq!(sims(&run.anisette), 1);
    assert_eq!(sims(&run.other_anisette[0]), 1, "the next server gives plain data, not the sim of another");
    let second: Vec<serde_json::Value> = run.other_anisette[0]
        .iter()
        .filter(|r| r.path == "/v3/get_headers")
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect();
    assert!(second[0].get("sim").is_none());
}

#[test]
fn a_server_that_cannot_synchronize_gives_way_to_the_next_one_which_never_sees_the_sim() {
    let mut apple = Apple::new(TwoFa::None);
    apple.ask_sync_otps = vec!["otp-a"];
    let run = run_scenario(Scenario {
        servers: vec!["otp-a", BEFORE_SYNC],
        initial_otp: "otp-a",
        failing_sync: vec![0],
        ..Scenario::new(apple)
    });
    run.result.as_ref().unwrap();
    // asked to synchronize, the server errors, the next server's plain data are used
    assert_eq!(run.operations(), ["init", "init", "complete"]);
    assert_eq!(otps(&run), ["otp-a", BEFORE_SYNC, BEFORE_SYNC]);
    let bodies = |requests: &[Req]| -> Vec<serde_json::Value> {
        requests
            .iter()
            .filter(|r| r.path == "/v3/get_headers")
            .map(|r| serde_json::from_slice(&r.body).unwrap())
            .collect()
    };
    let first = bodies(&run.anisette);
    assert_eq!(first.len(), 1);
    assert_eq!(first[0]["sim"], SIM, "the sim went to the server whose data Apple asked to synchronize");
    let second = bodies(&run.other_anisette[0]);
    assert_eq!(second.len(), 1);
    assert!(second[0].get("sim").is_none(), "the sim of one identity means nothing to another server");
    assert_eq!(run.remembered.as_deref(), Some(run.urls[1].as_str()));
}

#[test]
fn minus_45061_that_stays_after_a_new_identity_gets_the_data_of_the_next_server() {
    let mut apple = Apple::new(TwoFa::None);
    apple.stale_otp = Some("otp-stale");
    let run = run_scenario(Scenario {
        servers: vec!["otp-stale", BEFORE_SYNC],
        initial_otp: "otp-stale",
        ..Scenario::new(apple)
    });
    run.result.as_ref().unwrap();
    // -45061, a new identity (still -45061: the server gives the same data), then the next server
    assert_eq!(otps(&run), ["otp-stale", "otp-stale", BEFORE_SYNC, BEFORE_SYNC]);
    assert_eq!(run.provisioned.len(), 1, "one new identity with the first server, no loop");
    assert_ne!(run.identifier, [9u8; 16].to_vec(), "the identity with the first server was made again");
    assert_eq!(run.remembered.as_deref(), Some(run.urls[1].as_str()));
}

#[test]
fn a_server_that_is_down_is_skipped_before_anything_goes_to_apple() {
    let run = run_scenario(Scenario {
        dead_servers: 2,
        from_servers: true,
        ..Scenario::new(Apple::new(TwoFa::None))
    });
    run.result.as_ref().unwrap();
    // Apple only ever saw the data of the server that answers
    assert_eq!(otps(&run), [BEFORE_SYNC, BEFORE_SYNC]);
    assert_eq!(run.remembered.as_deref(), Some(run.urls[2].as_str()));
}

#[test]
fn an_unknown_apple_id_still_tells_which_server_apple_accepted() {
    // -20970: an answer about the account, so Apple took the data
    let mut apple = Apple::new(TwoFa::None);
    apple.refuse_init = Some((-20970, "Your account information was entered incorrectly."));
    let run = run_scenario(Scenario {
        servers: vec![BEFORE_SYNC, "otp-second"],
        ..Scenario::new(apple)
    });
    let error = run.result.as_ref().unwrap_err();
    assert!(matches!(error, Error::AuthSrpWithMessage(-20970, _)), "{error}");
    assert_eq!(run.service_requests().len(), 1, "an answer about the account is not a reason to try another server");
    assert_eq!(run.remembered.as_deref(), Some(run.urls[0].as_str()));
}

#[test]
fn every_request_carries_the_pair_the_server_announces_whatever_it_says() {
    for (client_info, user_agent) in [
        (CLIENT_INFO, USER_AGENT),
        (WINDOWS, WINDOWS_USER_AGENT),
        (ANDROID, ANDROID_USER_AGENT),
        ("<Quantum9000> <QuantumOS;42.0> <com.apple.AuthKit/1 (com.apple.akd/9.9)>", "akd/9.9 Whatever/1.0"),
    ] {
        let run = run_scenario(Scenario {
            announces: (client_info, user_agent),
            from_servers: true,
            answers: vec![code("123456")],
            ..Scenario::new(Apple::new(TwoFa::TrustedDevice))
        });
        run.result.as_ref().unwrap_or_else(|e| panic!("{client_info}: {e}"));
        assert!(run.apple.len() >= 6);
        for req in &run.apple {
            // the provisioning URL bag is asked with the Mac client info of its own, when the first one lacks it
            assert_eq!(req.headers["x-mme-client-info"], client_info, "{} {}", req.method, req.path);
            assert_eq!(req.headers["user-agent"], user_agent, "{} {}", req.method, req.path);
        }
    }
}

#[test]
fn the_override_replaces_the_client_info_and_user_agent_on_every_request() {
    let overrides = ClientOverrides::new(
        Some("<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"),
        Some("akd/1.0 CFNetwork/811.5.4"),
    );
    let run = run_scenario(Scenario {
        overrides,
        from_servers: true,
        answers: vec![TwoFactorAction::SendSms(1), code("123456")],
        ..Scenario::new(Apple::new(TwoFa::TrustedDevice))
    });
    run.result.as_ref().unwrap();
    // GrandSlam, the push, the SMS request that fails, the code: all of them, not only the login
    assert!(run.apple.len() >= 8, "{}", run.apple.len());
    for req in &run.apple {
        assert_eq!(
            req.headers["x-mme-client-info"],
            "<Mac15,7> <macOS;27.0;26A5378j> <com.apple.AuthKit/1 (com.apple.akd/1.0)>",
            "{} {}",
            req.method,
            req.path
        );
        assert_eq!(req.headers["user-agent"], "akd/1.0 CFNetwork/811.5.4", "{} {}", req.method, req.path);
    }
    // and nothing was overridden by default: the other tests of this file carry the server's pair
}

// ---------------------------------------------------------------- the developer portal

/// The answer of `listTeams.action` with no team: all that a session needs to be established.
fn teams_response() -> Resp {
    Resp::plist(dict(vec![
        ("creationTimestamp", string("2026-10-15T20:00:00Z")),
        ("resultCode", int(0)),
        ("userLocale", string("en_US")),
        ("protocolVersion", string("QH65B2")),
        ("responseId", string("response-1")),
        ("teams", Value::Array(Vec::new())),
    ]))
}

/// The portal's answer to a request whose anisette data it does not accept: the session is "expired".
fn session_expired() -> Resp {
    Resp::plist(dict(vec![
        ("creationTimestamp", string("2026-10-15T20:00:00Z")),
        ("resultCode", int(1100)),
        ("userString", string("Your session has expired. Please log in.")),
        ("userLocale", string("en_US")),
        ("protocolVersion", string("QH65B2")),
        ("responseId", string("response-1")),
    ]))
}

fn unavailable() -> Resp {
    Resp::html(503, "503 Service Temporarily Unavailable")
}

struct SessionRun {
    result: Result<crate::developer::DeveloperSession, Error>,
    portal: Vec<Req>,
    apple: Vec<Req>,
    anisette: Vec<Req>,
    /// What the anisette servers behind the first received, in order.
    other_anisette: Vec<Vec<Req>>,
    /// The URLs of the anisette servers, in order.
    urls: Vec<String>,
    /// The server the configuration folder remembers as the one the portal accepted.
    remembered: Option<String>,
}

impl Drop for SessionRun {
    /// The session may be used after it was opened, so Apple and the portal stay local until then.
    fn drop(&mut self) {
        TEST_GSA_HOST.with(|host| *host.borrow_mut() = None);
        crate::developer::TEST_DEVELOPER_HOST.with(|host| *host.borrow_mut() = None);
    }
}

impl SessionRun {
    fn error(&self) -> &Error {
        self.result.as_ref().err().expect("the session should not have been established")
    }

    fn portal_otps(&self) -> Vec<String> {
        self.portal
            .iter()
            .map(|r| r.headers.get("x-apple-i-md").cloned().unwrap_or_default())
            .collect()
    }
}

/// Open a developer session with anisette data that belongs to a `fresh_identity` or not. The portal
/// accepts the one-time password `accepts` only (any when `None`) and answers `refusal` to the others.
fn open_session(
    apple: Apple,
    fresh_identity: bool,
    accepts: Option<&'static str>,
    refusal: fn() -> Resp,
) -> SessionRun {
    open_session_with(apple, fresh_identity, accepts, refusal, vec![BEFORE_SYNC])
}

/// [`open_session`] with several anisette servers, which give these one-time passwords (the data the
/// session starts with are the first server's).
fn open_session_with(
    apple: Apple,
    fresh_identity: bool,
    accepts: Option<&'static str>,
    refusal: fn() -> Resp,
    otps: Vec<&'static str>,
) -> SessionRun {
    let apple = Arc::new(apple);
    let handler = apple.clone();
    let (apple_url, apple_seen) = serve(move |req| handler.handle(req));
    let servers: Vec<_> = otps.iter().map(|otp| anisette_server_giving(otp)).collect();
    let urls: Vec<String> = servers.iter().map(|server| server.0.clone()).collect();
    let (config, dir) = anisette_config(&urls.iter().map(String::as_str).collect::<Vec<_>>());
    let (portal_url, portal_seen) = serve(move |req| {
        let otp = req.headers.get("x-apple-i-md").map(String::as_str);
        if accepts.is_some() && otp != accepts {
            return refusal();
        }
        match req.path.as_str() {
            "/services/QH65B2/listTeams.action" => teams_response(),
            _ => unavailable(),
        }
    });

    let mut base_headers = HashMap::new();
    base_headers.insert("X-Mme-Client-Info".to_string(), CLIENT_INFO.to_string());
    base_headers.insert("X-Apple-I-MD".to_string(), otps[0].to_string());
    base_headers.insert("X-Apple-I-MD-M".to_string(), "machine".to_string());
    base_headers.insert("X-Mme-Device-Id".to_string(), "09090909-0909-0909-0909-090909090909".to_string());
    let anisette = AnisetteData {
        base_headers,
        generated_at: SystemTime::now(),
        config,
        user_agent: USER_AGENT.to_string(),
        fresh_identity,
        refresh_failed_at: None,
        server: Some(urls[0].clone()),
    };

    TEST_GSA_HOST.with(|host| *host.borrow_mut() = Some(apple_url));
    crate::developer::TEST_DEVELOPER_HOST.with(|host| *host.borrow_mut() = Some(portal_url));
    let result = block_on(crate::developer::DeveloperSession::new_synchronizing(
        "adsid-1".to_string(),
        "gs-token-1".to_string(),
        anisette,
    ));
    let run = SessionRun {
        result,
        portal: portal_seen.lock().unwrap().clone(),
        apple: apple_seen.lock().unwrap().clone(),
        anisette: servers[0].1.lock().unwrap().clone(),
        other_anisette: servers[1..].iter().map(|server| server.1.lock().unwrap().clone()).collect(),
        remembered: omnisette::remote_anisette_v3::remembered_server(&dir, &urls),
        urls,
    };
    let _ = std::fs::remove_dir_all(dir);
    run
}

#[test]
fn the_portal_sees_the_client_info_the_user_agent_and_the_anisette_data_of_the_server() {
    let run = open_session(Apple::new(TwoFa::None), false, None, unavailable);
    let session = run.result.as_ref().ok().expect("the session should be established");

    assert_eq!(run.portal.len(), 1);
    assert_eq!(run.remembered.as_deref(), Some(run.urls[0].as_str()), "the portal accepted this server's data");
    let h = &run.portal[0].headers;
    assert_eq!(h["x-mme-client-info"], CLIENT_INFO);
    assert_eq!(h["user-agent"], USER_AGENT);
    assert_eq!(h["x-apple-i-identity-id"], "adsid-1");
    assert_eq!(h["x-apple-gs-token"], "gs-token-1");
    assert_eq!(h["x-apple-app-info"], "com.apple.gs.xcode.auth");
    assert_eq!(h["x-xcode-version"], crate::auth::anisette_data::xcode_version());
    assert_eq!(h["x-apple-i-md"], BEFORE_SYNC);
    assert_eq!(h["x-apple-i-md-m"], "machine");
    assert_eq!(h["x-mme-device-id"], "09090909-0909-0909-0909-090909090909");
    assert_eq!(h["content-type"], "text/x-xml-plist");
    // the same on the other API of the portal, which answers with an HTML 503 page here: reported as
    // what it is, not as a "Serde JSON error"
    let url = crate::developer::developer_url("/v1/bundleIds");
    let error = block_on(session.v1_send_request(&url, None, None)).unwrap_err();
    assert!(matches!(&error, Error::DeveloperHttp { status: 503, .. }), "{error}");
    assert_eq!(
        error.to_string(),
        "Apple developer services answered HTTP 503 without a usable reply: 503 Service Temporarily Unavailable"
    );
    // no GrandSlam request for an identity that is not new
    assert!(run.apple.is_empty());
}

#[test]
fn a_portal_that_answers_503_to_the_data_of_a_server_gets_the_data_of_the_next() {
    let run = open_session_with(
        Apple::new(TwoFa::None),
        false,
        Some("otp-b"),
        unavailable,
        vec!["otp-a", "otp-b", "otp-c"],
    );
    assert!(run.result.is_ok(), "the session should be established with the second server");
    assert_eq!(run.portal_otps(), ["otp-a", "otp-b"], "the third server is not asked");
    assert!(run.other_anisette[1].is_empty());
    // the server the portal accepted is the one to try first next time
    assert_eq!(run.remembered.as_deref(), Some(run.urls[1].as_str()));
    // nothing went to GrandSlam: the identities are known, only the portal refused the first data
    assert!(run.apple.is_empty());
}

#[test]
fn a_portal_that_refuses_every_server_is_reported_after_one_request_to_each() {
    let run = open_session_with(
        Apple::new(TwoFa::None),
        false,
        Some("nothing-is-accepted"),
        unavailable,
        vec!["otp-a", "otp-b"],
    );
    let error = run.error();
    assert!(matches!(error, Error::DeveloperHttp { status: 503, .. }), "{error}");
    assert_eq!(run.portal_otps(), ["otp-a", "otp-b"]);
    assert_eq!(run.remembered, None);
}

#[test]
fn an_expired_session_answer_does_not_make_the_portal_change_servers() {
    // 1100 is the portal talking about the session, not about the data: no other server is asked
    let run = open_session_with(
        Apple::new(TwoFa::None),
        false,
        Some("nothing-is-accepted"),
        session_expired,
        vec!["otp-a", "otp-b"],
    );
    let error = run.error();
    assert!(matches!(error, Error::DeveloperApi { result_code: 1100, .. }), "{error}");
    assert_eq!(run.portal_otps(), ["otp-a"]);
    assert!(run.other_anisette[0].is_empty());
}

#[test]
fn an_html_503_from_the_portal_is_not_a_plist_error() {
    let run = open_session(Apple::new(TwoFa::None), false, Some("nothing-is-accepted"), unavailable);
    let error = run.error();
    assert!(matches!(error, Error::DeveloperHttp { status: 503, .. }), "{error}");
    assert!(error.to_string().contains("503 Service Temporarily Unavailable"), "{error}");
}

#[test]
fn a_new_identity_refused_by_the_portal_is_synchronized_and_the_request_sent_again() {
    let mut apple = Apple::new(TwoFa::None);
    apple.needs_sync = true;
    let run = open_session(apple, true, Some(AFTER_SYNC), session_expired);
    assert!(run.result.is_ok(), "the session should be established after the synchronization");

    // refused with the data Apple had never seen, accepted with the synchronized ones
    assert_eq!(run.portal_otps(), [BEFORE_SYNC, AFTER_SYNC]);
    // one request to GrandSlam, for a throwaway Apple ID, answered with the order to synchronize
    assert_eq!(run.apple.len(), 1);
    let body: Dictionary = plist::from_bytes(&run.apple[0].body).unwrap();
    let username = body
        .get("Request")
        .and_then(Value::as_dictionary)
        .and_then(|r| r.get("u"))
        .and_then(Value::as_string)
        .unwrap();
    assert!(username.starts_with("tvsign-sync-") && username.ends_with("@example.invalid"), "{username}");
    // the synchronization went through the anisette server, with the data of Apple
    let asked: Vec<serde_json::Value> = run
        .anisette
        .iter()
        .filter(|r| r.path == "/v3/get_headers")
        .map(|r| serde_json::from_slice(&r.body).unwrap())
        .collect();
    assert_eq!(asked.len(), 1);
    assert_eq!(asked[0]["sim"], SIM);
}

#[test]
fn an_identity_apple_knows_is_never_synchronized_again() {
    let mut apple = Apple::new(TwoFa::None);
    apple.needs_sync = true;
    let run = open_session(apple, false, Some(AFTER_SYNC), session_expired);
    // the session really is refused: this is what the caller sees, and Apple was not bothered
    let error = run.error();
    assert!(matches!(error, Error::DeveloperApi { result_code: 1100, .. }), "{error}");
    assert_eq!(run.portal.len(), 1);
    assert!(run.apple.is_empty());
    assert!(run.anisette.iter().all(|r| r.path != "/v3/get_headers"));
}

#[test]
fn a_new_identity_that_apple_does_not_ask_to_synchronize_is_not_touched() {
    // the portal refuses the session for its own reasons, GrandSlam knows the identity already
    let run = open_session(Apple::new(TwoFa::None), true, Some(AFTER_SYNC), session_expired);
    let error = run.error();
    assert!(matches!(error, Error::DeveloperApi { result_code: 1100, .. }), "{error}");
    // the first answer is the one that is reported; there was one question to GrandSlam and no retry
    assert_eq!(run.portal.len(), 1);
    assert_eq!(run.apple.len(), 1);
    assert!(run.anisette.iter().all(|r| r.path != "/v3/get_headers"));
}

// ---------------------------------------------------------------- the data of a conversation stay with one server

/// An anisette server like [`anisette_server_giving`] that can be taken down: while `down` is set it
/// answers 500 to everything.
fn switchable_server(
    otp: &'static str,
    down: Arc<std::sync::atomic::AtomicBool>,
) -> (String, Arc<Mutex<Vec<Req>>>) {
    let (url, seen, _) = serve_with_provisioning(move |req| {
        if down.load(std::sync::atomic::Ordering::SeqCst) {
            return Resp::status(500);
        }
        match req.path.as_str() {
            "/v3/client_info" => Resp::json(200, &format!(r#"{{"client_info":"{CLIENT_INFO}","user_agent":"{USER_AGENT}"}}"#)),
            "/v3/get_headers" => Resp::json(
                200,
                &format!(r#"{{"result":"Headers","X-Apple-I-MD-M":"machine","X-Apple-I-MD":"{otp}","X-Apple-I-MD-RINFO":"1"}}"#),
            ),
            _ => Resp::status(404),
        }
    });
    (url, seen)
}

#[test]
fn a_refresh_keeps_to_the_server_of_the_conversation_and_does_not_fail_over() {
    use std::sync::atomic::{AtomicBool, Ordering};
    // the user types a 2FA code for more than a minute: the next request goes out with fresh data. They
    // must be the data of the same server (the device identifier Apple knows this conversation by), and a
    // server that went down is an error, not another server's identity in the middle of the conversation
    let down = Arc::new(AtomicBool::new(false));
    let (a, _) = switchable_server("otp-a", down.clone());
    let (b, b_seen) = switchable_server("otp-b", Arc::new(AtomicBool::new(false)));
    let (config, dir) = anisette_config(&[&a, &b]);

    let data = block_on(AnisetteData::new(config)).unwrap();
    assert_eq!(data.server.as_deref(), Some(a.as_str()));
    let again = block_on(data.refresh()).unwrap();
    assert_eq!(again.server.as_deref(), Some(a.as_str()));
    assert_eq!(again.base_headers["X-Apple-I-MD"], "otp-a");
    // the sign-in still knows every server, in case Apple refuses these data later
    assert_eq!(again.config.configured_servers_v3(), [a.clone(), b.clone()]);

    down.store(true, Ordering::SeqCst);
    let error = block_on(data.refresh()).unwrap_err();
    assert!(matches!(error, Error::Anisette(_)), "{error}");
    assert!(b_seen.lock().unwrap().is_empty(), "the other server was never asked: {:?}", b_seen.lock().unwrap());
    // the start of a sign-in is where the free failover is
    let fresh = block_on(AnisetteData::new(data.config.clone().set_preferred_url_v3(None))).unwrap();
    assert_eq!(fresh.server.as_deref(), Some(b.as_str()));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn a_server_that_could_not_serve_is_left_alone_by_the_rest_of_the_sign_in() {
    // otherwise every switch of server would pay the connection timeout of the dead one again
    let dead = dead_url();
    let (good, _) = switchable_server("otp-good", Arc::new(std::sync::atomic::AtomicBool::new(false)));
    let (spare, _) = switchable_server("otp-spare", Arc::new(std::sync::atomic::AtomicBool::new(false)));
    let (config, dir) = anisette_config(&[&dead, &good, &spare]);

    let data = block_on(AnisetteData::new(config)).unwrap();
    assert_eq!(data.server.as_deref(), Some(good.as_str()));
    assert_eq!(data.config.servers_v3(), [good.clone(), spare.clone()], "the dead server is not tried again");
    // Apple refuses the data of the server in use: the next one is the spare, not the dead one first
    let next = block_on(data.from_other_server()).unwrap().expect("the spare is left");
    assert_eq!(next.server.as_deref(), Some(spare.as_str()));
    assert!(block_on(next.from_other_server()).unwrap().is_none(), "nothing left but servers already given up");
    let _ = std::fs::remove_dir_all(dir);
}
