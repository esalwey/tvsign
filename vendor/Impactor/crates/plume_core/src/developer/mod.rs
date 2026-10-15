mod platform;
pub mod qh;
mod session;
pub mod v1;

pub use platform::DeveloperPlatform;
pub use session::{DeveloperSession, RequestType};

#[cfg(test)]
thread_local! {
    /// The tests of the developer session point the portal at a local server (the whole session
    /// runs on the thread of the test).
    pub(crate) static TEST_DEVELOPER_HOST: std::cell::RefCell<Option<String>> = const { std::cell::RefCell::new(None) };
}

/// URL of an endpoint of the developer services (`https://developerservices2.apple.com/services`
/// followed by `endpoint`).
pub fn developer_url(endpoint: impl AsRef<str>) -> String {
    #[cfg(test)]
    if let Some(host) = TEST_DEVELOPER_HOST.with(|host| host.borrow().clone()) {
        return format!("{host}/services{}", endpoint.as_ref());
    }
    format!("https://developerservices2.apple.com/services{}", endpoint.as_ref())
}

#[macro_export]
macro_rules! developer_endpoint {
    ($endpoint:expr) => {
        $crate::developer::developer_url($endpoint)
    };
}

// Apple apis restrict certain characters in app names
pub fn strip_invalid_chars(s: &str) -> String {
    s.chars().filter(|c| c.is_ascii_alphabetic()).collect()
}
