use std::collections::HashMap;
use std::time::{Duration, Instant, SystemTime};

use omnisette::{AnisetteConfiguration, AnisetteHeaders};

use crate::Error;

#[derive(Debug, Clone)]
pub struct AnisetteData {
    pub base_headers: HashMap<String, String>,
    pub generated_at: SystemTime,
    pub config: AnisetteConfiguration,
    /// Dernier renouvellement abandonne apres ses essais : pendant `REFRESH_BACKOFF` on garde les
    /// anciennes donnees au lieu de retenter (et d'attendre) a chaque requete Apple.
    refresh_failed_at: Option<Instant>,
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
            config,
            refresh_failed_at: None,
        })
    }

    pub fn needs_refresh(&self) -> bool {
        // Horloge qui recule : on garde les donnees, jamais de panique.
        let elapsed = self.generated_at.elapsed().unwrap_or_default();
        elapsed.as_secs() > 60
    }

    pub fn is_valid(&self) -> bool {
        let elapsed = self.generated_at.elapsed().unwrap_or_default();
        elapsed.as_secs() < 90
    }

    pub async fn refresh(&self) -> Result<Self, crate::Error> {
        Self::new(self.config.clone()).await
    }

    /// Renouvelle les donnees anisette en place : 3 essais espaces, puis on garde les anciennes
    /// (Apple refusera peut-etre la requete, mais plumesign ne s'arrete plus sur une erreur passagere).
    pub async fn refresh_in_place(&mut self) {
        if self.in_backoff(Instant::now()) {
            return;
        }
        let mut delay = std::time::Duration::from_secs(1);
        for attempt in 1..=3 {
            match self.refresh().await {
                Ok(fresh) => {
                    *self = fresh;
                    return;
                }
                Err(e) => {
                    log::warn!("anisette refresh failed (attempt {attempt}/3): {e}");
                    if attempt < 3 {
                        tokio::time::sleep(delay).await;
                        delay *= 2;
                    }
                }
            }
        }
        log::warn!("anisette refresh abandoned, keeping the previous data");
        self.refresh_failed_at = Some(Instant::now());
    }

    /// Un renouvellement vient d'etre abandonne : inutile de retenter avant `REFRESH_BACKOFF`.
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
            // Donnees un peu perimees (anisette injoignable) : on tente quand meme, Apple tranchera.
            log::warn!("anisette data is older than 90 s");
        }

        let mut headers = self.base_headers.clone();
        let old_client_info = headers.remove("X-Mme-Client-Info");

        if client_info {
            let client_info = match old_client_info {
                Some(v) => {
                    let temp = v.as_str();

                    match temp.split('<').nth(3).and_then(|s| s.split('>').next()) {
                        Some(part) => {
                            temp.replace(part, "com.apple.AuthKit/1 (com.apple.akd/1.0)")
                        }
                        // Format inattendu : on garde l'en-tete tel quel.
                        None => temp.to_string(),
                    }
                }
                None => {
                    return headers;
                }
            };
            headers.insert("X-Mme-Client-Info".to_owned(), client_info.to_owned());
        }

        if app_info {
            headers.insert(
                "X-Apple-App-Info".to_owned(),
                "com.apple.gs.xcode.auth".to_owned(),
            );
            headers.insert("X-Xcode-Version".to_owned(), "11.2 (11B41)".to_owned());
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

    pub fn get_header(&self, header: &str) -> Result<String, Error> {
        let headers = self
            .generate_headers(true, true, true)
            .iter()
            .map(|(k, v)| (k.to_lowercase(), v.to_lowercase()))
            .collect::<HashMap<String, String>>();

        match headers.get(&header.to_lowercase()) {
            Some(v) => Ok(v.to_string()),
            None => Err(Error::DeveloperSessionRequestFailed),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> AnisetteData {
        AnisetteData {
            base_headers: HashMap::new(),
            generated_at: SystemTime::now(),
            config: AnisetteConfiguration::default(),
            refresh_failed_at: None,
        }
    }

    #[test]
    fn no_backoff_before_any_failure() {
        assert!(!sample().in_backoff(Instant::now()));
    }

    #[test]
    fn a_failed_refresh_pauses_new_attempts_for_a_while() {
        let mut data = sample();
        let failed = Instant::now();
        data.refresh_failed_at = Some(failed);
        assert!(data.in_backoff(failed + Duration::from_secs(5)));
        assert!(data.in_backoff(failed + REFRESH_BACKOFF - Duration::from_secs(1)));
        assert!(!data.in_backoff(failed + REFRESH_BACKOFF));
    }
}
