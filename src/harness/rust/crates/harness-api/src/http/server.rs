//! TCP serving for the harness HTTP boundary.

use std::net::SocketAddr;

use axum::Router;

/// Bind `addr` and serve `router` until the process stops.
///
/// # Errors
///
/// Returns an I/O error when the listener cannot bind or the server fails.
pub async fn serve(router: Router, addr: SocketAddr) -> std::io::Result<()> {
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, router).await
}
