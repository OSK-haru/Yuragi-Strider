use axum::http::HeaderValue;
use std::env;
use yuragi_strider_api::api_router;

#[tokio::main]
async fn main() {
    let port = env::var("PORT")
        .ok()
        .and_then(|value| value.parse::<u16>().ok())
        .unwrap_or(8000);
    let allowed_origins = env::var("ALLOWED_ORIGINS")
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|origin| !origin.is_empty())
        .map(|origin| {
            HeaderValue::from_str(origin)
                .unwrap_or_else(|_| panic!("ALLOWED_ORIGINS contains an invalid header value"))
        })
        .collect();

    let address = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&address)
        .await
        .unwrap_or_else(|error| panic!("failed to bind {address}: {error}"));
    axum::serve(listener, api_router(allowed_origins))
        .await
        .expect("Rust API server failed");
}
