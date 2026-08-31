# Cloud Storage & Protocol Adapters — ChunkGuard

> **Audience**: Cloud Architects, Systems Engineers, DevOps
> **Status**: Architecture & Protocol Specification
> **Reading time**: ~15 minutes

---

## 1. Overview

ChunkGuard is designed as a protocol-agnostic, fault-tolerant data transfer engine. While standard HTTP/1.1 and HTTP/2 Range requests serve as the default transport layer, ChunkGuard provides native protocol adapters for **Cloud Object Stores (AWS S3, Google Cloud Storage, Azure Blob Storage)**, **Enterprise Proxies (HTTP CONNECT & SOCKS5)**, and **Next-Generation HTTP/3 (QUIC)**.

```
                         ┌─────────────────────────────┐
                         │   ChunkGuard Core Engine    │
                         └──────────────┬──────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │  Abstract Storage Adapter   │
                         │    (BaseStorageAdapter)     │
                         └──────────────┬──────────────┘
                                        │
         ┌──────────────────┬───────────┴───────────┬──────────────────┐
         │                  │                       │                  │
   ┌─────▼──────┐     ┌─────▼──────┐          ┌─────▼──────┐     ┌─────▼──────┐
   │ HTTP/HTTPS │     │  AWS S3    │          │ Google GCS │     │ Azure Blob │
   │  Adapter   │     │  Adapter   │          │  Adapter   │     │  Adapter   │
   └─────┬──────┘     └─────┬──────┘          └─────┬──────┘     └─────┬──────┘
         │                  │                       │                  │
         ▼                  ▼                       ▼                  ▼
  [ HTTP/2 / H3 ]    [ s3:// URIs ]          [ gs:// URIs ]     [ az:// URIs ]
  [ SOCKS5/Proxy]    [ SigV4 Range]          [ GCS Resumable]   [ Blob Range ]
```

---

## 2. Abstract Storage Adapter Interface

Every protocol adapter implements `BaseStorageAdapter`:

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator

class BaseStorageAdapter(ABC):
    """Abstract interface for all transport and storage providers."""

    @abstractmethod
    async def get_metadata(self, uri: str) -> ObjectMetadata:
        """Fetch file size, ETag, and capability matrix without downloading payload."""
        pass

    @abstractmethod
    async def fetch_range(
        self,
        uri: str,
        start_byte: int,
        end_byte: int
    ) -> AsyncIterator[bytes]:
        """Stream byte range asynchronously."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Release underlying connection pools and sockets."""
        pass
```

---

## 3. Amazon Web Services (AWS S3) Adapter

### 3.1 Supported URI Formats
* `s3://bucket-name/path/to/object.tar.gz`
* `https://bucket-name.s3.region.amazonaws.com/path/to/object.tar.gz`
* Presigned S3 GET URLs

### 3.2 Authentication & Credentials
The S3 adapter evaluates AWS credentials in standard precedence order:
1. Explicit CLI/Config keys (`aws_access_key_id`, `aws_secret_access_key`, `aws_session_token`)
2. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`)
3. AWS Shared Credentials file (`~/.aws/credentials`)
4. AWS IAM Instance Profile / ECS Task Role / EKS IRSA (IAM Roles for Service Accounts)

### 3.3 Protocol Execution & Range Requests
S3 natively supports the HTTP `Range: bytes=start-end` header. ChunkGuard signs each individual chunk request with **AWS Signature Version 4 (SigV4)**:

```http
GET /large-dataset.parquet HTTP/1.1
Host: my-data-bucket.s3.us-east-1.amazonaws.com
Range: bytes=16777216-33554431
x-amz-date: 20260824T080000Z
x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20260824/us-east-1/s3/aws4_request, SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, Signature=...
```

### 3.4 S3 Express One Zone & Cross-Region Acceleration
ChunkGuard automatically detects and leverages **S3 Transfer Acceleration** (`bucketname.s3-accelerate.amazonaws.com`) and low-latency **S3 Express One Zone** endpoints when configured.

---

## 4. Google Cloud Storage (GCS) Adapter

### 4.1 Supported URI Formats
* `gs://bucket-name/path/to/file.img`
* `https://storage.googleapis.com/bucket-name/path/to/file.img`

### 4.2 Authentication
* Google Application Default Credentials (ADC)
* Service Account JSON key file via `GOOGLE_APPLICATION_CREDENTIALS`
* Workload Identity in Google Kubernetes Engine (GKE)

### 4.3 Range Request Implementation
ChunkGuard uses OAuth2 Bearer tokens with GCS JSON/XML APIs and passes standard `Range: bytes=start-end` headers. For files with MD5 or CRC32C metadata, ChunkGuard maps and cross-validates checksums alongside SHA-256.

---

## 5. Microsoft Azure Blob Storage Adapter

### 5.1 Supported URI Formats
* `az://container-name/blob-name`
* `https://account.blob.core.windows.net/container/blob`

### 5.2 Authentication
* Shared Access Signature (SAS) token
* Storage Account Shared Key
* Azure Active Directory / Azure Managed Identity via `DefaultAzureCredential`

### 5.3 Range Request Implementation
Uses Azure Blob REST API version `2023-11-03` with `x-ms-range: bytes=start-end` or standard `Range` headers.

---

## 6. Enterprise Proxy & Tunneling Subsystem

In secure corporate networks, downloads must route through forward proxies with authentication and SSL inspection.

```
ChunkGuard ──▶ [ HTTP CONNECT Tunnel ] ──▶ [ Corporate Proxy ] ──▶ Target Server
                     or SOCKS5 Handshake           (Auth & Inspection)
```

### 6.1 Supported Proxy Types
1. **HTTP/HTTPS Forward Proxy**: Standard `http://proxy:8080` with `CONNECT` tunnel for HTTPS traffic.
2. **SOCKS5 Proxy (RFC 1928)**: `socks5://proxy:1080` or `socks5h://proxy:1080` (remote DNS resolution over proxy to prevent DNS leaks).

### 6.2 Proxy Configuration

```yaml
# chunkguard.yaml
network:
  proxy:
    http_proxy: "http://proxy.corp.internal:8080"
    https_proxy: "http://user:password@proxy.corp.internal:8080"
    socks_proxy: "socks5h://127.0.0.1:9050"
    no_proxy: "localhost,127.0.0.1,.internal.corp"
    ssl_ca_bundle: "/etc/ssl/certs/corp-root-ca.pem"
```

CLI Override:
```bash
chunkguard download \
  --proxy "http://proxy.corp.internal:8080" \
  --ca-bundle "/etc/ssl/certs/corp-root-ca.pem" \
  "https://releases.example.com/build.iso" "./build.iso"
```

---

## 7. HTTP/3 (QUIC) Transport

ChunkGuard supports HTTP/3 (over QUIC/UDP) for environments with packet loss and high latency.

### 7.1 Benefits of HTTP/3 for ChunkGuard
* **Zero Head-of-Line Blocking**: In HTTP/2 (over TCP), a single lost packet stalls all multiplexed streams in the TCP window. In HTTP/3 (over QUIC), packet loss on chunk $A$ has zero impact on chunk $B$.
* **0-RTT Connection Resumption**: Instant connection establishment for resumed transfers.
* **Connection Migration**: Coroutine workers seamlessly continue chunk downloads when client IP/network changes (e.g. WiFi to cellular handoff).

### 7.2 Fallback Strategy
```
Attempt HTTP/3 (QUIC / UDP)
      │
      ├── Timeout / UDP Blocked ──▶ Fallback to HTTP/2 (TCP / TLS)
      │
      └── HTTP/2 Rejected      ──▶ Fallback to HTTP/1.1 Keep-Alive
```
