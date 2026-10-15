
use std::collections::HashMap;

use crate::AnisetteError;

#[cfg_attr(feature = "async", async_trait::async_trait)]
pub trait AnisetteHeadersProvider: Send + Sync {
    #[cfg_attr(not(feature = "async"), remove_async_await::remove_async_await)]
    async fn get_anisette_headers(
        &mut self,
        skip_provisioning: bool,
    ) -> Result<HashMap<String, String>, AnisetteError>;

    #[cfg_attr(not(feature = "async"), remove_async_await::remove_async_await)]
    async fn get_authentication_headers(&mut self) -> Result<HashMap<String, String>, AnisetteError> {
        let headers = self.get_anisette_headers(false).await?;
        Ok(self.normalize_headers(headers))
    }

    /// Normalizes headers to ensure that all the required headers are given.
    fn normalize_headers(
        &mut self,
        mut headers: HashMap<String, String>,
    ) -> HashMap<String, String> {
        if let Some(client_info) = headers.remove("X-MMe-Client-Info") {
            headers.insert("X-Mme-Client-Info".to_string(), client_info);
        }

        headers
    }

    /// User-Agent that goes with the `X-Mme-Client-Info` of the headers: Apple checks that the two
    /// describe the same device. `None` when the provider has no opinion.
    fn user_agent(&self) -> Option<String> {
        None
    }

    /// True when the device identity was provisioned while producing the last headers, so Apple has
    /// not seen it synchronize yet.
    fn identity_is_new(&self) -> bool {
        false
    }

    /// The server that produced the last headers, for a provider that chooses between several.
    fn server_used(&self) -> Option<String> {
        None
    }

    /// Headers again, after GrandSlam asked for a synchronization (`hsc` 434 with an
    /// `X-Apple-I-MD-DATA` value, passed as `sim`).
    #[cfg_attr(not(feature = "async"), remove_async_await::remove_async_await)]
    async fn resync_anisette_headers(
        &mut self,
        _sim: &str,
    ) -> Result<HashMap<String, String>, AnisetteError> {
        Err(AnisetteError::ResyncUnsupported)
    }
}
