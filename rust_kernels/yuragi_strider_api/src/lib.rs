use axum::extract::{DefaultBodyLimit, Request};
use axum::http::header::{
    CONTENT_SECURITY_POLICY, REFERRER_POLICY, X_CONTENT_TYPE_OPTIONS, X_FRAME_OPTIONS,
};
use axum::http::{HeaderValue, Method};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{Value, json};
use tower_http::cors::{AllowOrigin, CorsLayer};

mod circuit_compile;

const API_CONTENT_SECURITY_POLICY_VALUE: &str =
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'";
pub const MAX_API_REQUEST_BODY_BYTES: usize = 64 * 1024;

/// Parse exact browser origins, optionally requiring HTTPS for a production deployment.
pub fn parse_allowed_origins(raw: &str, require_https: bool) -> Result<Vec<HeaderValue>, String> {
    let mut origins = Vec::new();
    for item in raw
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
    {
        let (scheme, authority) = item
            .split_once("://")
            .ok_or_else(|| format!("ALLOWED_ORIGINS contains an invalid origin: {item}"))?;
        let scheme_valid = if require_https {
            scheme == "https"
        } else {
            matches!(scheme, "http" | "https")
        };
        if !scheme_valid
            || authority.is_empty()
            || authority.chars().any(|character| {
                character.is_ascii_whitespace()
                    || matches!(character, '/' | '?' | '#' | '@' | '\\' | ',')
            })
        {
            return Err(format!(
                "ALLOWED_ORIGINS contains an invalid origin: {item}"
            ));
        }
        let value = HeaderValue::from_str(item)
            .map_err(|_| format!("ALLOWED_ORIGINS contains an invalid header value: {item}"))?;
        if !origins.contains(&value) {
            origins.push(value);
        }
    }
    Ok(origins)
}

/// Build the API router. The supplied origins are the exact allowlist from
/// `ALLOWED_ORIGINS`; an empty list preserves the current no-CORS behavior.
pub fn api_router(allowed_origins: Vec<HeaderValue>) -> Router {
    let mut router = Router::new()
        .route("/api/health", get(health))
        .route("/api/circuit/compile", post(compile_circuit));

    if !allowed_origins.is_empty() {
        router = router.layer(
            CorsLayer::new()
                .allow_origin(AllowOrigin::list(allowed_origins))
                .allow_methods([Method::GET, Method::POST])
                .allow_headers(tower_http::cors::Any),
        );
    }

    router
        .layer(DefaultBodyLimit::max(MAX_API_REQUEST_BODY_BYTES))
        .layer(middleware::from_fn(add_api_security_headers))
}

async fn health() -> Json<Value> {
    Json(json!({
        "status": "ok",
        "pulse_models": [
            "driven_coupled_transmon_network_rwa_experimental_v1",
            "driven_transmon_qutrit_rwa_experimental_v1",
            "driven_two_level_rwa_experimental_v1"
        ],
        "pulse_model_statuses": {
            "driven_coupled_transmon_network_rwa_experimental_v1": "experimental",
            "driven_transmon_qutrit_rwa_experimental_v1": "available",
            "driven_two_level_rwa_experimental_v1": "available"
        }
    }))
}

async fn compile_circuit(Json(request): Json<Value>) -> Response {
    match circuit_compile::compile_preview(&request) {
        Ok(response) => Json(response).into_response(),
        Err(error) => (error.status, Json(json!({"detail": error.detail}))).into_response(),
    }
}

async fn add_api_security_headers(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(
        CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(API_CONTENT_SECURITY_POLICY_VALUE),
    );
    headers.insert(X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    headers.insert(X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    headers.insert(REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    headers.insert(
        "permissions-policy",
        HeaderValue::from_static("camera=(), geolocation=(), microphone=()"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::{MAX_API_REQUEST_BODY_BYTES, api_router, parse_allowed_origins};
    use axum::body::to_bytes;
    use axum::http::header::{
        ACCESS_CONTROL_ALLOW_METHODS, ACCESS_CONTROL_ALLOW_ORIGIN, ACCESS_CONTROL_REQUEST_METHOD,
        CONTENT_SECURITY_POLICY, ORIGIN,
    };
    use axum::http::{HeaderValue, Method, Request, StatusCode};
    use serde_json::Value;
    use tower::ServiceExt;

    const HEALTH_CONTRACT: &str = include_str!("../contracts/api-health-v1.json");
    const BELL_DIRECT_CONTRACT: &str =
        include_str!("../contracts/circuit-compile-bell-logical-direct-v1.json");
    const AUTO_DECOMPOSE_REQUEST: &str =
        include_str!("../contracts/circuit-compile-auto-decompose-request-v1.json");
    const AUTO_DECOMPOSE_CONTRACT: &str =
        include_str!("../contracts/circuit-compile-auto-decompose-v1.json");

    #[tokio::test]
    async fn health_response_matches_the_shared_wire_contract() {
        let response = api_router(Vec::new())
            .oneshot(
                Request::builder()
                    .uri("/api/health")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers()[CONTENT_SECURITY_POLICY],
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        );
        assert_eq!(response.headers()["x-content-type-options"], "nosniff");
        assert_eq!(response.headers()["x-frame-options"], "DENY");
        assert_eq!(response.headers()["referrer-policy"], "no-referrer");
        assert_eq!(
            response.headers()["permissions-policy"],
            "camera=(), geolocation=(), microphone=()"
        );

        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let actual: Value = serde_json::from_slice(&bytes).unwrap();
        let expected: Value = serde_json::from_str(HEALTH_CONTRACT).unwrap();
        assert_eq!(actual, expected);
    }

    #[tokio::test]
    async fn health_preflight_preserves_the_configured_cors_policy() {
        let origin = HeaderValue::from_static("https://ui.example.test");
        let response = api_router(vec![origin])
            .oneshot(
                Request::builder()
                    .method(Method::OPTIONS)
                    .uri("/api/health")
                    .header(ORIGIN, "https://ui.example.test")
                    .header(ACCESS_CONTROL_REQUEST_METHOD, "GET")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(
            response.headers()[ACCESS_CONTROL_ALLOW_ORIGIN],
            "https://ui.example.test"
        );
        assert!(
            response.headers()[ACCESS_CONTROL_ALLOW_METHODS]
                .to_str()
                .unwrap()
                .split(',')
                .any(|method| method.trim() == "GET")
        );
    }

    #[tokio::test]
    async fn health_preflight_does_not_allow_an_unconfigured_origin() {
        let response = api_router(vec![HeaderValue::from_static("https://ui.example.test")])
            .oneshot(
                Request::builder()
                    .method(Method::OPTIONS)
                    .uri("/api/health")
                    .header(ORIGIN, "https://untrusted.example.test")
                    .header(ACCESS_CONTROL_REQUEST_METHOD, "GET")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert!(!response.headers().contains_key(ACCESS_CONTROL_ALLOW_ORIGIN));
    }

    #[tokio::test]
    async fn circuit_compile_route_returns_the_shared_bell_preview_contract() {
        let response = api_router(Vec::new())
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/circuit/compile")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(r#"{"circuit_preset":"bell"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let actual: Value = serde_json::from_slice(&bytes).unwrap();
        let expected: Value = serde_json::from_str(BELL_DIRECT_CONTRACT).unwrap();
        assert_eq!(actual, expected);
    }

    #[tokio::test]
    async fn circuit_compile_route_serves_auto_decomposition() {
        let response = api_router(Vec::new())
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/circuit/compile")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(AUTO_DECOMPOSE_REQUEST))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let actual: Value = serde_json::from_slice(&bytes).unwrap();
        let expected: Value = serde_json::from_str(AUTO_DECOMPOSE_CONTRACT).unwrap();
        assert_eq!(actual, expected);
    }

    #[tokio::test]
    async fn circuit_compile_rejects_request_bodies_over_the_api_budget() {
        let oversized = format!(
            "{{\"padding\":\"{}\"}}",
            "x".repeat(MAX_API_REQUEST_BODY_BYTES)
        );
        let response = api_router(Vec::new())
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/circuit/compile")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(oversized))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(response.headers()["x-content-type-options"], "nosniff");
    }

    #[test]
    fn origin_configuration_is_exact_and_production_requires_https() {
        assert_eq!(
            parse_allowed_origins(" https://ui.example.test,https://ui.example.test ", true)
                .unwrap()
                .len(),
            1
        );
        assert!(parse_allowed_origins("http://localhost:5173", true).is_err());
        assert!(parse_allowed_origins("https://ui.example.test/path", false).is_err());
        assert!(parse_allowed_origins("*", false).is_err());
    }
}
