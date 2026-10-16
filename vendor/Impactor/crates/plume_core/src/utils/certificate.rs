use std::{
    collections::VecDeque,
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
    vec,
};

use apple_codesign::{
    SigningSettings,
    cryptography::{InMemoryPrivateKey, PrivateKey},
};
// TODO: why do we have pem and pem_rfc7468 deps again?
use pem_rfc7468::{LineEnding, encode_string};
use rand::rngs::OsRng;
use rcgen::{DnType, KeyPair, PKCS_RSA_SHA256};
use rsa::{
    RsaPrivateKey,
    pkcs1::EncodeRsaPublicKey,
    pkcs8::{DecodePrivateKey, EncodePrivateKey},
};
use x509_certificate::{CapturedX509Certificate, X509Certificate};

use crate::{
    Error,
    developer::{DeveloperSession, qh::certs::Cert},
};

pub(crate) const MACHINE_NAME: &str = "AltStore";

/// Un certificat de developpement qui expire dans moins de ce delai est remplace a l'avance.
const RENEW_MARGIN: Duration = Duration::from_secs(30 * 24 * 3600);
/// Nombre maximal de revocations tentees pour liberer une place (evite toute boucle).
const MAX_REVOKE_ATTEMPTS: usize = 6;

/// Quels certificats le code a le droit de revoquer quand le compte est a sa limite (erreur 7460).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RevokeMode {
    /// Rien de vivant : seuls les certificats deja expires ou revoques (sans effet) sont touches.
    Never,
    /// Les certificats crees par cette installation (meme nom de machine), plus les morts.
    OwnOnly,
    /// Tout, y compris Xcode, SideStore, AltStore, une autre installation. Ancien comportement.
    Any,
}

impl RevokeMode {
    /// Lit « never », « own » ou « any » (et quelques synonymes), sans tenir compte de la casse.
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "never" | "no" | "none" | "0" | "false" => Some(Self::Never),
            "own" | "own-only" | "own_only" | "ours" => Some(Self::OwnOnly),
            "any" | "all" | "yes" | "1" | "true" => Some(Self::Any),
            _ => None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct CertificatePolicy {
    pub revoke: RevokeMode,
    /// Prefixe commun des noms de machine des autres installations de la meme famille
    /// (ex. « tvsign » pour « tvsign-1a2b3c4d »). Sert a les classer avant les certificats etrangers.
    pub family_prefix: Option<String>,
}

impl CertificatePolicy {
    /// Comportement historique (interface graphique : l'utilisateur confirme la remise a zero).
    pub fn legacy() -> Self {
        Self {
            revoke: RevokeMode::Any,
            family_prefix: None,
        }
    }
}

impl Default for CertificatePolicy {
    fn default() -> Self {
        Self {
            revoke: RevokeMode::OwnOnly,
            family_prefix: None,
        }
    }
}

/// Ce qu'on sait d'un certificat du compte, sans les champs prives de l'API.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct CertSummary {
    pub serial: String,
    pub name: String,
    pub machine_name: Option<String>,
    pub status: String,
    pub expires: SystemTime,
}

impl CertSummary {
    fn from_cert(cert: &Cert) -> Self {
        Self {
            serial: cert.serial_number.clone(),
            name: cert.name.clone(),
            machine_name: cert.machine_name.clone(),
            status: cert.status.clone(),
            expires: cert.expiration_date.into(),
        }
    }

    fn is_issued(&self) -> bool {
        self.status.eq_ignore_ascii_case("issued")
    }

    /// Deja revoque : le revoquer de nouveau ne libere aucune place.
    fn is_revoked(&self) -> bool {
        self.status.eq_ignore_ascii_case("revoked")
    }

    /// Revoque ou expire (statut ou date) : le revoquer ne casse plus aucune app. Un statut inconnu
    /// n'est PAS mort : on ne prend pas un champ Apple mal orthographie pour une autorisation.
    pub(crate) fn is_dead(&self, now: SystemTime) -> bool {
        self.is_revoked() || self.status.eq_ignore_ascii_case("expired") || self.expires <= now
    }

    /// Utilisable pour signer, avec au moins `margin` de validite restante.
    pub(crate) fn usable_for(&self, now: SystemTime, margin: Duration) -> bool {
        self.is_issued() && self.expires > now + margin
    }

    pub(crate) fn describe(&self) -> String {
        format!(
            "{} (machine {}, serial {}, {}, expires {})",
            self.name,
            self.machine_name.as_deref().unwrap_or("?"),
            self.serial,
            self.status,
            format_date(self.expires)
        )
    }
}

/// AAAA-MM-JJ (UTC) d'un instant, sans dependance de calendrier.
pub(crate) fn format_date(time: SystemTime) -> String {
    let secs = time
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let days = secs.div_euclid(86_400);
    // Algorithme « civil from days » de H. Hinnant.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!("{y:04}-{m:02}-{d:02}")
}

/// Rang d'un certificat pour la revocation, du moins grave au plus grave.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum CertTier {
    /// Expire ou revoque : rien ne casse.
    Dead,
    /// Cree par cette installation.
    Own,
    /// Cree par une autre installation de la meme famille (un autre tvsign).
    Family,
    /// Xcode, SideStore, AltStore...
    Foreign,
}

fn tier_of(
    c: &CertSummary,
    own_machine: &str,
    family: Option<&str>,
    now: SystemTime,
) -> CertTier {
    if c.is_dead(now) {
        CertTier::Dead
    } else if c.machine_name.as_deref() == Some(own_machine) {
        CertTier::Own
    } else if family.is_some_and(|prefix| {
        c.machine_name
            .as_deref()
            .is_some_and(|m| m.to_ascii_lowercase().starts_with(prefix))
    }) {
        CertTier::Family
    } else {
        CertTier::Foreign
    }
}

/// Certificats que la politique autorise a revoquer, dans l'ordre : morts d'abord, puis les notres,
/// puis ceux d'une autre installation de la famille, puis les etrangers (Xcode, SideStore...).
/// Dans chaque groupe, le plus proche de l'expiration d'abord. Les certificats deja « Revoked » sont
/// exclus : les revoquer encore ne libererait rien.
pub(crate) fn revocation_candidates(
    certs: &[CertSummary],
    own_machine: &str,
    policy: &CertificatePolicy,
    now: SystemTime,
) -> Vec<(CertTier, CertSummary)> {
    let family = policy
        .family_prefix
        .as_deref()
        .filter(|p| !p.is_empty())
        .map(str::to_ascii_lowercase);

    let mut out: Vec<(CertTier, CertSummary)> = certs
        .iter()
        .filter(|c| !c.is_revoked())
        .map(|c| (tier_of(c, own_machine, family.as_deref(), now), c.clone()))
        .filter(|(t, _)| match policy.revoke {
            RevokeMode::Never => *t == CertTier::Dead,
            RevokeMode::OwnOnly => *t <= CertTier::Own,
            RevokeMode::Any => true,
        })
        .collect();
    out.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.expires.cmp(&b.1.expires)));
    out
}

/// File des revocations a tenter. Seuls les certificats vivants comptent dans la limite
/// `MAX_REVOKE_ATTEMPTS` : retirer un certificat mort ne risque rien et ne doit pas empecher de
/// remplacer un de nos certificats vivants.
pub(crate) struct RevokePlan {
    candidates: VecDeque<(CertTier, CertSummary)>,
    live_attempts: usize,
}

impl RevokePlan {
    pub(crate) fn new(candidates: Vec<(CertTier, CertSummary)>) -> Self {
        Self {
            candidates: candidates.into(),
            live_attempts: 0,
        }
    }

    /// Le prochain certificat a revoquer, ou `None` quand il n'y en a plus ou que la limite est atteinte.
    pub(crate) fn next(&mut self) -> Option<(CertTier, CertSummary)> {
        let (tier, _) = self.candidates.front()?;
        if *tier != CertTier::Dead {
            if self.live_attempts >= MAX_REVOKE_ATTEMPTS {
                return None;
            }
            self.live_attempts += 1;
        }
        self.candidates.pop_front()
    }
}

/// Ligne de journal d'une revocation. Seule la mention « (FOREIGN) » signale qu'un certificat d'une
/// autre application est tombe : tvsign (app/plume.py) previent alors l'utilisateur.
fn revocation_message(tier: CertTier, candidate: &CertSummary) -> String {
    let what = match tier {
        CertTier::Dead => "Removed dead certificate",
        CertTier::Own => "Revoked own certificate",
        CertTier::Family | CertTier::Foreign => "Revoked certificate (FOREIGN)",
    };
    format!(
        "{what} with serial number {} [{}]",
        candidate.serial,
        candidate.describe()
    )
}

/// Etat d'une cle locale face aux certificats du compte.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum KeyState {
    /// Certificat « Issued » et loin de l'expiration : on le reutilise.
    Reusable,
    /// Encore valide mais proche de la fin : on tente de le remplacer, sinon on le garde.
    Expiring,
    /// Revoque, expire ou statut inconnu : inutilisable.
    Unusable,
}

pub(crate) fn key_state(cert: &CertSummary, now: SystemTime) -> KeyState {
    if cert.usable_for(now, RENEW_MARGIN) {
        KeyState::Reusable
    } else if cert.usable_for(now, Duration::ZERO) {
        KeyState::Expiring
    } else {
        KeyState::Unusable
    }
}

/// Parmi les cles locales retrouvees (dans l'ordre : `key.pem` puis la cle en attente), laquelle
/// reutiliser et laquelle a renouveler. Retourne les indices `(reutilisable, a renouveler)`.
/// Le premier reutilisable l'emporte ; une cle en attente reutilisable est donc adoptee.
pub(crate) fn pick_existing_key(
    found: &[CertSummary],
    now: SystemTime,
) -> (Option<usize>, Option<usize>) {
    let mut expiring = None;
    for (i, summary) in found.iter().enumerate() {
        match key_state(summary, now) {
            KeyState::Reusable => return (Some(i), expiring),
            KeyState::Expiring => {
                expiring.get_or_insert(i);
            }
            KeyState::Unusable => {}
        }
    }
    (None, expiring)
}

/// Ecrit `data` dans `path` sans jamais laisser un fichier tronque : fichier temporaire voisin, puis renommage.
/// Sous Unix le fichier est en 0600 (il contient une cle privee).
pub(crate) fn write_atomic(path: &Path, data: &[u8]) -> std::io::Result<()> {
    use std::io::Write;

    let mut tmp_name = path
        .file_name()
        .map(|n| n.to_os_string())
        .unwrap_or_default();
    tmp_name.push(".tmp");
    let tmp = path.with_file_name(tmp_name);

    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let result = (|| {
        let mut file = options.open(&tmp)?;
        file.write_all(data)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

pub struct CertificateIdentity {
    pub cert: Option<CapturedX509Certificate>,
    pub key: Option<Box<dyn PrivateKey>>,
    pub machine_id: Option<String>,
    pub serial_number: Option<String>,
    pub p12_data: Option<Vec<u8>>,
    pub new: bool,
    /// Fin de validite du certificat Apple utilise, quand elle est connue.
    pub expires_at: Option<SystemTime>,
}

impl CertificateIdentity {
    // Use for cli context or if you actually store pems? why would you do that though
    pub async fn new_with_paths(paths: Option<Vec<PathBuf>>) -> Result<Self, Error> {
        let mut cert = Self {
            cert: None,
            key: None,
            machine_id: None,
            p12_data: None,
            serial_number: None,
            new: false,
            expires_at: None,
        };

        if let Some(paths) = paths {
            for path in &paths {
                let pem_data = fs::read(path)?;
                cert.resolve_certificate_from_contents(pem_data)?;
            }
        }

        Ok(cert)
    }

    /// Comportement historique : revoque n'importe quel certificat si le compte est plein.
    pub async fn new_with_session(
        session: &DeveloperSession,
        config_path: PathBuf,
        machine_name: Option<String>,
        team_id: &String,
        is_export: bool,
        on_certificate_reset: Option<&mut dyn FnMut() -> bool>,
    ) -> Result<Self, Error> {
        Self::new_with_session_policy(
            session,
            config_path,
            machine_name,
            team_id,
            is_export,
            CertificatePolicy::legacy(),
            on_certificate_reset,
        )
        .await
    }

    pub async fn new_with_session_policy(
        session: &DeveloperSession,
        config_path: PathBuf,
        machine_name: Option<String>,
        team_id: &String,
        is_export: bool,
        policy: CertificatePolicy,
        on_certificate_reset: Option<&mut dyn FnMut() -> bool>,
    ) -> Result<Self, Error> {
        let machine_name = machine_name.unwrap_or_else(|| MACHINE_NAME.to_string());

        let key_dir = Self::key_dir(config_path, &team_id)?;
        let key_path = key_dir.join("key.pem");
        // Cle generee avant l'envoi du CSR : si la suite echoue, le prochain passage retrouve le certificat.
        let pending_path = key_dir.join("key.pending.pem");

        let mut identity = Self {
            cert: None,
            key: None,
            machine_id: None,
            p12_data: None,
            serial_number: None,
            new: false,
            expires_at: None,
        };

        // To same some unnecessary requests, we're going to list our certificates first here
        // then pass them into the necessary functions that need it, if the functions absolutely
        // need to request certificates (after submitting a CSR, for example), they can do so
        let certs = session.qh_list_certs(&team_id).await?.certificates;
        let now = SystemTime::now();

        // Only the key will be written to disk, certificate can just be gotten via the request
        // request we've made, by trying to match our public key with the requests public key.
        // Un certificat n'est reutilise que s'il est « Issued » et loin de son expiration.
        let mut found: Vec<(RsaPrivateKey, Cert, bool)> = Vec::new();
        for (path, is_pending) in [(&key_path, false), (&pending_path, true)] {
            if !path.exists() {
                continue;
            }
            let priv_key = match fs::read_to_string(path)
                .map_err(Error::from)
                .and_then(|s| RsaPrivateKey::from_pkcs8_pem(&s).map_err(Error::from))
            {
                Ok(key) => key,
                Err(e) => {
                    log::warn!("unreadable private key {}: {e}", path.display());
                    continue;
                }
            };
            let Some(cert) = Self::find_certificate(&certs, &priv_key)? else {
                continue;
            };
            found.push((priv_key, cert, is_pending));
        }
        let summaries: Vec<CertSummary> = found
            .iter()
            .map(|(_, cert, _)| CertSummary::from_cert(cert))
            .collect();
        for summary in &summaries {
            match key_state(summary, now) {
                KeyState::Reusable => {}
                KeyState::Expiring => log::warn!(
                    "certificate {} expires on {}, replacing it",
                    summary.serial,
                    format_date(summary.expires)
                ),
                KeyState::Unusable => log::warn!(
                    "certificate {} is no longer usable ({} / {}), requesting a new one",
                    summary.serial,
                    summary.status,
                    format_date(summary.expires)
                ),
            }
        }
        let (reusable_index, expiring_index) = pick_existing_key(&summaries, now);
        let mut found: Vec<Option<(RsaPrivateKey, Cert, bool)>> =
            found.into_iter().map(Some).collect();
        let reusable = reusable_index.and_then(|i| found[i].take());
        let expiring = expiring_index
            .and_then(|i| found[i].take())
            .map(|(priv_key, cert, _)| (priv_key, cert));

        let (cert, priv_key) = if let Some((priv_key, cert, is_pending)) = reusable {
            if is_pending {
                // Un passage precedent a cree le certificat sans pouvoir finir : on le reprend.
                Self::promote_key(&pending_path, &key_path, &priv_key)?;
            }
            (cert, priv_key)
        } else {
            // Un certificat encore valide mais proche de la fin : on tente de le remplacer sans rien
            // revoquer (il sert encore), et on garde l'ancien si le compte n'a pas de place.
            let renewal_policy = if expiring.is_some() {
                CertificatePolicy {
                    revoke: RevokeMode::Never,
                    ..policy.clone()
                }
            } else {
                policy.clone()
            };
            match identity
                .request_new_certificate(
                    session,
                    team_id,
                    &machine_name,
                    certs,
                    &renewal_policy,
                    &pending_path,
                    &key_path,
                    on_certificate_reset,
                )
                .await
            {
                Ok((cert, priv_key)) => {
                    identity.new = true;
                    (cert, priv_key)
                }
                Err(e) => match expiring {
                    Some((priv_key, cert)) => {
                        log::warn!("could not renew the expiring certificate, keeping it: {e}");
                        (cert, priv_key)
                    }
                    None => return Err(e),
                },
            }
        };

        // We need to save the machine_id for our P12
        if let Some(ref machine_id) = cert.machine_id {
            identity.set_machine_id(machine_id.clone());
        }
        identity.set_serial_number(cert.serial_number.clone());
        let expires: SystemTime = cert.expiration_date.into();
        identity.expires_at = Some(expires);
        log::info!(
            "Using certificate {} (expires {})",
            cert.serial_number,
            format_date(expires)
        );

        let cert_pem = encode_string(
            "CERTIFICATE",
            LineEnding::LF,
            cert.cert_content
                .ok_or(Error::CertificatePemMissing)?
                .as_ref(),
        )
        .map_err(|_| Error::CertificatePemMissing)?;
        let key_pem = priv_key.to_pkcs8_pem(Default::default())?.to_string();
        let key_pair: [Vec<u8>; 2] = [cert_pem.into_bytes(), key_pem.into_bytes()];

        // TODO: this may be horrendious
        if let Some(p12_data) = identity.create_pkcs12(&key_pair, is_export) {
            identity.p12_data = Some(p12_data);
        }

        for pem in key_pair {
            identity.resolve_certificate_from_contents(pem)?;
        }

        Ok(identity)
    }

    /// Date de fin du certificat Apple (AAAA-MM-JJ, UTC), quand elle est connue.
    pub fn expires_on(&self) -> Option<String> {
        self.expires_at.map(format_date)
    }

    /// `key.pending.pem` devient `key.pem` (ecriture atomique), puis disparait.
    fn promote_key(pending: &Path, key_path: &Path, priv_key: &RsaPrivateKey) -> Result<(), Error> {
        let key_pem = priv_key.to_pkcs8_pem(Default::default())?.to_string();
        write_atomic(key_path, key_pem.as_bytes())?;
        let _ = fs::remove_file(pending);
        Ok(())
    }

    // <config_path>/keys/<team_id>
    fn key_dir(path: PathBuf, team_id: &String) -> Result<PathBuf, Error> {
        let dir = path.join("keys").join(team_id);

        fs::create_dir_all(&dir)?;

        Ok(dir)
    }

    fn set_machine_id(&mut self, machine_id: String) {
        self.machine_id = Some(machine_id);
    }

    fn set_serial_number(&mut self, serial_number: String) {
        self.serial_number = Some(serial_number);
    }

    // TODO: cleanest p12 code of them all
    // the main horror about p12 creation is that we rely on p12-keystore which is
    // just another unnecessary dependency, but the p12 crate that applecodesign-rs
    // uses has no support for modern encryption, hopefully this doesn't add that
    // much more bloat
    pub fn create_pkcs12(&self, data: &[Vec<u8>; 2], is_export: bool) -> Option<Vec<u8>> {
        let cert_der = pem::parse(&data[0]).ok()?.contents().to_vec();
        let key_der = pem::parse(&data[1]).ok()?.contents().to_vec();

        let cert = p12_keystore::Certificate::from_der(&cert_der).ok()?;

        let local_key_id = {
            use sha1::{Digest, Sha1};
            let mut hasher = Sha1::new();
            hasher.update(&key_der);
            let hash = hasher.finalize();
            hash[..8].to_vec()
        };

        let key_chain = p12_keystore::PrivateKeyChain::new(key_der, local_key_id, vec![cert]);

        let mut keystore = p12_keystore::KeyStore::new();
        keystore.add_entry(
            "plume",
            p12_keystore::KeyStoreEntry::PrivateKeyChain(key_chain),
        );

        // when exporting the user has no idea what the password is, just dont set one
        // otherwise, when not exporting (used for SideStore/AltStore) we use the
        // machine_id since it needs it to locate a matching certificate
        let password = if is_export {
            "".to_string()
        } else {
            self.machine_id.as_deref().unwrap_or("").to_string()
        };

        let writer = keystore.writer(&password);
        writer.write().ok()
    }

    // applecodesign-rs needs our contents as strings to sign
    fn resolve_certificate_from_contents(&mut self, contents: Vec<u8>) -> Result<(), Error> {
        for pem in pem::parse_many(contents).map_err(Error::Pem)? {
            match pem.tag() {
                "CERTIFICATE" => {
                    self.cert = Some(CapturedX509Certificate::from_der(pem.contents())?);
                }
                "PRIVATE KEY" => {
                    self.key = Some(Box::new(InMemoryPrivateKey::from_pkcs8_der(
                        pem.contents(),
                    )?));
                }
                "RSA PRIVATE KEY" => {
                    self.key = Some(Box::new(InMemoryPrivateKey::from_pkcs1_der(
                        pem.contents(),
                    )?));
                }
                tag => log::debug!("(unhandled PEM tag {}; ignoring)", tag),
            }
        }

        Ok(())
    }
}

impl CertificateIdentity {
    /// Le certificat du compte dont la cle publique est celle de `priv_key` (la cle fait foi : le nom
    /// de machine peut varier d'une installation a l'autre). Le statut et l'expiration sont juges par l'appelant.
    fn find_certificate(certs: &[Cert], priv_key: &RsaPrivateKey) -> Result<Option<Cert>, Error> {
        let pub_key_der_obj = priv_key.to_public_key().to_pkcs1_der()?.as_bytes().to_vec();

        for cert in certs {
            if let Some(cert_content) = &cert.cert_content {
                let Ok(parsed_cert) = X509Certificate::from_der(&cert_content) else {
                    continue;
                };
                if pub_key_der_obj == parsed_cert.public_key_data().as_ref() {
                    return Ok(Some(cert.clone()));
                }
            }
        }

        Ok(None)
    }

    async fn request_new_certificate(
        &mut self,
        session: &DeveloperSession,
        team_id: &String,
        machine_name: &String,
        certs: Vec<Cert>,
        policy: &CertificatePolicy,
        pending_path: &Path,
        key_path: &Path,
        mut on_certificate_reset: Option<&mut dyn FnMut() -> bool>,
    ) -> Result<(Cert, RsaPrivateKey), Error> {
        let priv_key = RsaPrivateKey::new(&mut OsRng, 2048)?;
        let priv_key_der = priv_key.to_pkcs8_der()?;
        let priv_key_pair = KeyPair::from_der(priv_key_der.as_bytes())?;

        // La cle est mise de cote AVANT l'envoi du CSR : si Apple cree le certificat et que la suite
        // echoue, la cle existe encore et le prochain passage retrouve le certificat (aucune place perdue).
        let key_pem = priv_key.to_pkcs8_pem(Default::default())?.to_string();
        write_atomic(pending_path, key_pem.as_bytes())?;

        let mut params = rcgen::CertificateParams::new(vec![]);
        params.alg = &PKCS_RSA_SHA256;
        params.key_pair = Some(priv_key_pair);

        let dn = &mut params.distinguished_name;
        dn.push(DnType::CountryName, "US");
        dn.push(DnType::StateOrProvinceName, "STATE");
        dn.push(DnType::LocalityName, "LOCAL");
        dn.push(DnType::OrganizationName, "ORGNIZATION");
        dn.push(DnType::CommonName, "CN");

        let cert_csr = rcgen::Certificate::from_params(params)?.serialize_request_pem()?;

        let summaries: Vec<CertSummary> = certs.iter().map(CertSummary::from_cert).collect();
        let mut plan = RevokePlan::new(revocation_candidates(
            &summaries,
            machine_name,
            policy,
            SystemTime::now(),
        ));
        let mut warned_about_reset = false;

        // When we submit a CSR theres a high chance of it failing, at least
        // on free developer accounts. On error 7460 (too many certificates) we revoke one
        // certificate the policy allows, then retry; each serial is tried once, live certificates at
        // most MAX_REVOKE_ATTEMPTS times in all (dead ones do not count). With nothing left to revoke we stop with CERT_LIMIT.
        let cert_id = loop {
            match session
                .qh_submit_cert_csr(&team_id, cert_csr.clone(), machine_name)
                .await
            {
                Ok(id) => break id,
                Err(e) => {
                    // 7460 is for too many certificates (I think)
                    if matches!(&e, Error::DeveloperApi { result_code, .. } if *result_code == 7460)
                    {
                        if !warned_about_reset {
                            if let Some(callback) = on_certificate_reset.as_deref_mut() {
                                if !callback() {
                                    return Err(Error::Certificate(
                                        "Certificate reset cancelled".into(),
                                    ));
                                }
                            }
                            warned_about_reset = true;
                        }

                        let mut revoked_any = false;
                        while let Some((tier, candidate)) = plan.next() {
                            match session.qh_revoke_cert(&team_id, &candidate.serial).await {
                                Ok(_) => {
                                    log::warn!("{}", revocation_message(tier, &candidate));
                                    revoked_any = true;
                                    break;
                                }
                                Err(err) => log::warn!(
                                    "Could not revoke certificate {}: {err}",
                                    candidate.serial
                                ),
                            }
                        }

                        if revoked_any {
                            continue;
                        }
                        return Err(certificate_limit_error(&summaries, machine_name, policy));
                    }

                    return Err(e);
                }
            }
        }
        .cert_request;

        // We need to save the machine_id for our P12
        if let Some(ref machine_id) = cert_id.machine_id {
            self.set_machine_id(machine_id.clone());
        }

        self.set_serial_number(cert_id.serial_num.clone());

        // We request again, and hope this has our new certificate ready: on laisse a Apple
        // quelques secondes pour l'afficher, la cle en attente couvre le cas ou il n'apparait pas.
        let cert = Self::wait_for_certificate(session, team_id, &cert_id.certificate_id).await?;

        // Cle definitive, ecrite de facon atomique.
        Self::promote_key(pending_path, key_path, &priv_key)?;

        Ok((cert, priv_key))
    }

    async fn wait_for_certificate(
        session: &DeveloperSession,
        team_id: &String,
        certificate_id: &str,
    ) -> Result<Cert, Error> {
        let mut last_error = Error::CertificatePemMissing;
        for attempt in 0..5u64 {
            if attempt > 0 {
                tokio::time::sleep(Duration::from_secs(attempt * 2)).await;
            }
            match session.qh_list_certs(team_id).await {
                Ok(list) => {
                    if let Some(cert) = list
                        .certificates
                        .into_iter()
                        .find(|c| c.certificate_id == certificate_id && c.cert_content.is_some())
                    {
                        return Ok(cert);
                    }
                    last_error = Error::CertificatePemMissing;
                }
                Err(e) => last_error = e,
            }
        }
        Err(last_error)
    }
}

/// Erreur « compte plein » : explique ce qui bloque et liste les certificats, pour que l'utilisateur
/// decide (CERT_LIMIT est repris tel quel par tvsign).
pub(crate) fn certificate_limit_error(
    certs: &[CertSummary],
    own_machine: &str,
    policy: &CertificatePolicy,
) -> Error {
    let listing = certs
        .iter()
        .map(CertSummary::describe)
        .collect::<Vec<_>>()
        .join("; ");
    let mode = match policy.revoke {
        RevokeMode::Never => "never",
        RevokeMode::OwnOnly => "own",
        RevokeMode::Any => "any",
    };
    Error::Certificate(format!(
        "CERT_LIMIT: the Apple account holds the maximum number of development certificates and none \
         may be revoked automatically (revoke mode: {mode}, this install: {own_machine}). \
         Existing certificates: {listing}. Revoke one yourself at developer.apple.com, or allow tvsign \
         to revoke other certificates (PLUMESIGN_REVOKE=any)."
    ))
}

impl CertificateIdentity {
    pub fn load_into_signing_settings<'settings, 'slf: 'settings>(
        &'slf self,
        settings: &'settings mut SigningSettings<'slf>,
    ) -> Result<(), Error> {
        let signing_cert = self.cert.clone().ok_or(Error::CertificatePemMissing)?;
        let signing_key = self.key.as_ref().ok_or(Error::CertificatePemMissing)?;

        settings.set_signing_key(signing_key.as_key_info_signer(), signing_cert);
        settings.chain_apple_certificates();
        settings.set_team_id_from_signing_certificate();

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DAY: u64 = 24 * 3600;

    fn at(days: u64) -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(days * DAY)
    }

    fn cert(serial: &str, machine: Option<&str>, status: &str, expires_day: u64) -> CertSummary {
        CertSummary {
            serial: serial.into(),
            name: format!("iOS Development: {serial}"),
            machine_name: machine.map(str::to_string),
            status: status.into(),
            expires: at(expires_day),
        }
    }

    fn serials(list: &[(CertTier, CertSummary)]) -> Vec<&str> {
        list.iter().map(|(_, c)| c.serial.as_str()).collect()
    }

    #[test]
    fn dead_certificates_are_revocable_in_every_mode() {
        let now = at(1000);
        let certs = vec![
            cert("X", Some("Xcode"), "Issued", 1500),
            cert("EXP", Some("AltStore"), "Issued", 900),
            cert("EXS", Some("Xcode"), "Expired", 2000),
            cert("REV", Some("Xcode"), "Revoked", 2000),
        ];
        let policy = CertificatePolicy {
            revoke: RevokeMode::Never,
            family_prefix: None,
        };
        let list = revocation_candidates(&certs, "tvsign-1", &policy, now);
        // An already revoked certificate frees nothing, so it is not a candidate.
        assert_eq!(serials(&list), vec!["EXP", "EXS"]);
    }

    #[test]
    fn an_unknown_status_is_never_taken_for_dead() {
        let now = at(1000);
        let certs = vec![
            cert("ODD", Some("Xcode"), "Pending", 1500),
            cert("ODD2", Some("tvsign-1"), "", 1500),
        ];
        assert!(!certs[0].is_dead(now));
        for revoke in [RevokeMode::Never, RevokeMode::OwnOnly] {
            let policy = CertificatePolicy {
                revoke,
                family_prefix: None,
            };
            let list = revocation_candidates(&certs, "tvsign-1", &policy, now);
            if revoke == RevokeMode::Never {
                assert!(list.is_empty());
            } else {
                // Only the certificate of this install, never the foreign one.
                assert_eq!(serials(&list), vec!["ODD2"]);
            }
        }
        assert_eq!(key_state(&certs[0], now), KeyState::Unusable);
        assert!(cert("E", None, "EXPIRED", 1500).is_dead(now));
        assert!(cert("R", None, "revoked", 1500).is_dead(now));
    }

    #[test]
    fn dead_certificates_do_not_use_up_the_revoke_attempts() {
        let now = at(1000);
        let mut certs: Vec<CertSummary> = (0..8)
            .map(|i| cert(&format!("D{i}"), Some("Xcode"), "Expired", 900 + i))
            .collect();
        certs.push(cert("MINE", Some("tvsign-1"), "Issued", 1500));
        certs.push(cert("OTHER", Some("Xcode"), "Issued", 1500));
        let policy = CertificatePolicy::default();
        let mut plan = RevokePlan::new(revocation_candidates(&certs, "tvsign-1", &policy, now));
        let mut got = Vec::new();
        while let Some((tier, c)) = plan.next() {
            got.push((tier, c.serial));
        }
        assert_eq!(got.len(), 9);
        assert_eq!(got[8], (CertTier::Own, "MINE".to_string()));
    }

    #[test]
    fn live_revocations_are_capped() {
        let now = at(1000);
        let certs: Vec<CertSummary> = (0..10)
            .map(|i| cert(&format!("L{i}"), Some("tvsign-1"), "Issued", 1100 + i))
            .collect();
        let policy = CertificatePolicy::default();
        let mut plan = RevokePlan::new(revocation_candidates(&certs, "tvsign-1", &policy, now));
        let mut count = 0;
        while plan.next().is_some() {
            count += 1;
        }
        assert_eq!(count, MAX_REVOKE_ATTEMPTS);
    }

    #[test]
    fn only_foreign_revocations_carry_the_foreign_marker() {
        let c = cert("S", Some("Xcode"), "Issued", 1500);
        assert!(!revocation_message(CertTier::Dead, &c).contains("Revoked certificate"));
        assert!(!revocation_message(CertTier::Own, &c).contains("Revoked certificate"));
        assert!(revocation_message(CertTier::Foreign, &c).contains("Revoked certificate (FOREIGN)"));
        assert!(revocation_message(CertTier::Family, &c).contains("Revoked certificate (FOREIGN)"));
    }

    #[test]
    fn existing_key_choice() {
        let now = at(1000);
        let valid = cert("V", None, "Issued", 1100);
        let soon = cert("S", None, "Issued", 1010);
        let gone = cert("G", None, "Revoked", 1100);
        // A valid key.pem wins.
        assert_eq!(pick_existing_key(&[valid.clone(), gone.clone()], now), (Some(0), None));
        // key.pem unusable: the pending key is adopted.
        assert_eq!(pick_existing_key(&[gone.clone(), valid.clone()], now), (Some(1), None));
        // Expiring, with no reusable one: renewal is attempted.
        assert_eq!(pick_existing_key(&[soon.clone(), gone.clone()], now), (None, Some(0)));
        // Expiring key.pem but a valid pending key: the pending key is used.
        assert_eq!(pick_existing_key(&[soon, valid], now), (Some(1), Some(0)));
        assert_eq!(pick_existing_key(&[gone], now), (None, None));
        assert_eq!(pick_existing_key(&[], now), (None, None));
    }

    #[test]
    fn own_only_never_touches_foreign_certificates() {
        let now = at(1000);
        let certs = vec![
            cert("XCODE", Some("Xcode"), "Issued", 1100),
            cert("ALT", Some("AltStore"), "Issued", 1200),
            cert("MINE", Some("tvsign-1"), "Issued", 1300),
            cert("OTHER", Some("tvsign-2"), "Issued", 1050),
        ];
        let policy = CertificatePolicy {
            revoke: RevokeMode::OwnOnly,
            family_prefix: Some("tvsign".into()),
        };
        let list = revocation_candidates(&certs, "tvsign-1", &policy, now);
        assert_eq!(serials(&list), vec!["MINE"]);
    }

    #[test]
    fn any_mode_orders_dead_then_own_then_family_then_foreign() {
        let now = at(1000);
        let certs = vec![
            cert("XCODE", Some("Xcode"), "Issued", 1100),
            cert("OTHER", Some("tvsign-2"), "Issued", 1500),
            cert("MINE", Some("tvsign-1"), "Issued", 1300),
            cert("DEAD", Some("Xcode"), "Expired", 1400),
            cert("ALT", Some("AltStore"), "Issued", 1050),
        ];
        let policy = CertificatePolicy {
            revoke: RevokeMode::Any,
            family_prefix: Some("tvsign".into()),
        };
        let list = revocation_candidates(&certs, "tvsign-1", &policy, now);
        assert_eq!(serials(&list), vec!["DEAD", "MINE", "OTHER", "ALT", "XCODE"]);
    }

    #[test]
    fn usability_needs_status_and_margin() {
        let now = at(1000);
        let margin = Duration::from_secs(30 * DAY);
        assert!(cert("A", None, "Issued", 1100).usable_for(now, margin));
        assert!(!cert("A", None, "Issued", 1010).usable_for(now, margin));
        assert!(cert("A", None, "Issued", 1010).usable_for(now, Duration::ZERO));
        assert!(!cert("A", None, "Revoked", 1100).usable_for(now, Duration::ZERO));
        assert!(!cert("A", None, "Issued", 999).usable_for(now, Duration::ZERO));
        assert!(cert("A", None, "Issued", 999).is_dead(now));
        assert!(cert("A", None, "issued", 1100).usable_for(now, margin));
    }

    #[test]
    fn revoke_mode_parsing() {
        assert_eq!(RevokeMode::parse("ANY"), Some(RevokeMode::Any));
        assert_eq!(RevokeMode::parse(" own "), Some(RevokeMode::OwnOnly));
        assert_eq!(RevokeMode::parse("never"), Some(RevokeMode::Never));
        assert_eq!(RevokeMode::parse("maybe"), None);
    }

    #[test]
    fn dates_are_formatted_in_utc() {
        assert_eq!(format_date(SystemTime::UNIX_EPOCH), "1970-01-01");
        assert_eq!(
            format_date(SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000)),
            "2023-11-14"
        );
        // 29 fevrier d'une annee bissextile
        assert_eq!(format_date(at(19_782)), "2024-02-29");
    }

    #[test]
    fn limit_error_lists_the_certificates() {
        let certs = vec![
            cert("S1", Some("Xcode"), "Issued", 1100),
            cert("S2", Some("AltStore"), "Issued", 1200),
        ];
        let err = certificate_limit_error(&certs, "tvsign-1", &CertificatePolicy::default());
        let text = err.to_string();
        assert!(text.contains("CERT_LIMIT"));
        assert!(text.contains("S1") && text.contains("S2"));
        assert!(text.contains("Xcode") && text.contains("AltStore"));
    }

    #[test]
    fn atomic_write_replaces_and_leaves_no_temp_file() {
        let dir = std::env::temp_dir().join(format!("plume-cert-test-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("key.pem");
        write_atomic(&path, b"first").unwrap();
        write_atomic(&path, b"second").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"second");
        let leftovers: Vec<_> = fs::read_dir(&dir)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name() != "key.pem")
            .collect();
        assert!(leftovers.is_empty());
        fs::remove_dir_all(&dir).ok();
    }
}
