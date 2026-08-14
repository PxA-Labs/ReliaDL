# Testing Strategy — ChunkGuard

> **Audience**: QA Engineers, Developers
> **Reading time**: ~10 minutes

---

## 1. Testing Philosophy

| Principle | Application |
|---|---|
| **Test at the right level** | Unit tests for logic, integration tests for I/O, E2E tests for workflows |
| **Deterministic tests** | No flaky tests — mock randomness, time, network |
| **Fast feedback** | Unit tests < 5s total, integration < 30s, E2E < 2 min |
| **Fault injection** | Explicitly test failure scenarios, not just happy paths |
| **Coverage targets** | 90%+ line coverage, 80%+ branch coverage |

---

## 2. Test Pyramid

```
                    ┌───────┐
                    │  E2E  │          ~10 tests
                    │ Tests │          Real HTTP, real files
                   ┌┴───────┴┐
                   │Integrat.│         ~30 tests
                   │ Tests   │         Mock HTTP, real files
                  ┌┴─────────┴┐
                  │   Unit    │        ~100+ tests
                  │   Tests   │        Pure logic, no I/O
                  └───────────┘
```

---

## 3. Unit Tests

### 3.1 Chunk Manager Tests

```python
# tests/unit/test_chunk_manager.py

class TestChunkComputation:
    """Test chunk boundary calculations."""
    
    def test_exact_division(self):
        """File size evenly divisible by chunk size."""
        chunks = compute_chunks(file_size=32_000_000, chunk_size=8_000_000)
        assert len(chunks) == 4
        assert chunks[0].start_byte == 0
        assert chunks[0].end_byte == 7_999_999
        assert chunks[3].end_byte == 31_999_999
    
    def test_remainder_chunk(self):
        """Last chunk is smaller when file size not evenly divisible."""
        chunks = compute_chunks(file_size=25_000_000, chunk_size=8_000_000)
        assert len(chunks) == 4
        assert chunks[3].size == 1_000_000  # 25M - 3*8M = 1M
    
    def test_single_chunk(self):
        """File smaller than chunk size → single chunk."""
        chunks = compute_chunks(file_size=1_000_000, chunk_size=8_000_000)
        assert len(chunks) == 1
        assert chunks[0].size == 1_000_000
    
    def test_empty_file(self):
        """Zero-byte file → empty chunk list."""
        chunks = compute_chunks(file_size=0, chunk_size=8_000_000)
        assert len(chunks) == 0
    
    def test_chunk_contiguity(self):
        """All chunks are contiguous with no gaps or overlaps."""
        chunks = compute_chunks(file_size=100_000_000, chunk_size=8_000_000)
        for i in range(1, len(chunks)):
            assert chunks[i].start_byte == chunks[i-1].end_byte + 1
    
    def test_chunk_coverage(self):
        """Union of all chunks covers the entire file."""
        file_size = 100_000_000
        chunks = compute_chunks(file_size=file_size, chunk_size=8_000_000)
        assert chunks[0].start_byte == 0
        assert chunks[-1].end_byte == file_size - 1
    
    def test_auto_adjust_chunk_size(self):
        """Chunk size increased when chunk count exceeds maximum."""
        # 1 TB file with 1 MB chunks = 1M chunks > 100K max
        chunks = compute_chunks(file_size=1_000_000_000_000, chunk_size=1_048_576)
        assert len(chunks) <= 100_000
```

### 3.2 Hash Verifier Tests

```python
# tests/unit/test_hash_verifier.py

class TestHashComputation:
    """Test SHA-256 hash computation."""
    
    def test_known_hash(self):
        """Verify hash of known input."""
        data = b"Hello, ChunkGuard!"
        expected = hashlib.sha256(data).hexdigest()
        computed = compute_hash(data)
        assert computed == expected
    
    def test_empty_input(self):
        """Hash of empty bytes."""
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert compute_hash(b"") == expected
    
    def test_streaming_matches_bulk(self):
        """Streaming hash produces same result as bulk hash."""
        data = os.urandom(10_000_000)  # 10 MB random data
        bulk_hash = hashlib.sha256(data).hexdigest()
        
        # Simulate streaming in 64KB chunks
        streaming_hash = compute_hash_streaming(
            chunks=[data[i:i+65536] for i in range(0, len(data), 65536)]
        )
        assert streaming_hash == bulk_hash
    
    def test_single_bit_change_detected(self):
        """Changing one bit produces a completely different hash."""
        data = b"A" * 1000
        original_hash = compute_hash(data)
        
        modified = bytearray(data)
        modified[500] ^= 0x01  # Flip one bit
        modified_hash = compute_hash(bytes(modified))
        
        assert original_hash != modified_hash
    
    def test_constant_time_comparison(self):
        """Hash comparison uses constant-time function."""
        hash_a = "a" * 64
        hash_b = "b" * 64
        # Should use hmac.compare_digest internally
        assert not verify_hash(hash_a, hash_b)
```

### 3.3 Retry Handler Tests

```python
# tests/unit/test_retry_handler.py

class TestBackoffComputation:
    """Test exponential backoff delay calculation."""
    
    def test_first_retry_delay(self):
        """First retry uses base delay."""
        policy = RetryPolicy(base_delay=1.0, backoff_factor=2.0, jitter_factor=0)
        delay = compute_delay(attempt=0, policy=policy)
        assert delay == 1.0
    
    def test_exponential_growth(self):
        """Delay doubles each attempt (without jitter)."""
        policy = RetryPolicy(base_delay=1.0, backoff_factor=2.0, jitter_factor=0)
        assert compute_delay(0, policy) == 1.0
        assert compute_delay(1, policy) == 2.0
        assert compute_delay(2, policy) == 4.0
        assert compute_delay(3, policy) == 8.0
    
    def test_max_delay_cap(self):
        """Delay never exceeds max_delay."""
        policy = RetryPolicy(base_delay=1.0, backoff_factor=2.0, 
                            max_delay=10.0, jitter_factor=0)
        assert compute_delay(100, policy) == 10.0
    
    def test_jitter_adds_randomness(self):
        """Jitter makes delays non-deterministic."""
        policy = RetryPolicy(base_delay=1.0, backoff_factor=2.0, jitter_factor=0.5)
        delays = [compute_delay(2, policy) for _ in range(100)]
        assert len(set(delays)) > 1  # Not all the same
    
    def test_retryable_classification(self):
        """Correctly classify retryable vs non-retryable errors."""
        policy = RetryPolicy()
        assert policy.is_retryable(TimeoutError())
        assert policy.is_retryable(ConnectionError())
        assert not policy.is_retryable(FileNotFoundError())
    
    def test_retryable_status_codes(self):
        """Correctly classify retryable HTTP status codes."""
        policy = RetryPolicy()
        assert policy.is_retryable_status(500)
        assert policy.is_retryable_status(503)
        assert policy.is_retryable_status(429)
        assert not policy.is_retryable_status(404)
        assert not policy.is_retryable_status(403)
```

### 3.4 Configuration Tests

```python
# tests/unit/test_config.py

class TestConfiguration:
    def test_default_values(self):
        config = DownloadConfig()
        assert config.chunk_size_bytes == 8_388_608
        assert config.max_parallel_workers == 4
    
    def test_size_parsing(self):
        assert parse_size("8MB") == 8_388_608
        assert parse_size("1GB") == 1_073_741_824
        assert parse_size("512KB") == 524_288
    
    def test_invalid_chunk_size(self):
        with pytest.raises(ConfigurationError):
            DownloadConfig(chunk_size_bytes=100).validate()  # Below 1MB minimum
    
    def test_invalid_workers(self):
        with pytest.raises(ConfigurationError):
            DownloadConfig(max_parallel_workers=0).validate()
        with pytest.raises(ConfigurationError):
            DownloadConfig(max_parallel_workers=100).validate()  # Above 32 max
```

---

## 4. Integration Tests

### 4.1 State Persistence Tests

```python
# tests/integration/test_state_manager.py

class TestStatePersistence:
    """Test state file read/write with real filesystem."""
    
    async def test_save_and_load(self, tmp_path):
        """State survives save→load cycle."""
        state = create_test_state(num_chunks=100)
        manager = StateManager(tmp_path / "test.state")
        
        await manager.save(state)
        loaded = await manager.load()
        
        assert loaded.download_id == state.download_id
        assert len(loaded.chunks) == 100
    
    async def test_atomic_write_survives_crash(self, tmp_path):
        """State file is not corrupted if write is interrupted."""
        state_path = tmp_path / "test.state"
        manager = StateManager(state_path)
        
        # Save initial good state
        await manager.save(create_test_state(num_chunks=10))
        
        # Simulate crash during save (write partial file, don't rename)
        with pytest.raises(SimulatedCrashError):
            await manager.save_with_crash(create_test_state(num_chunks=20))
        
        # Load should return the old good state
        loaded = await manager.load()
        assert len(loaded.chunks) == 10  # Not 20
    
    async def test_resume_resets_downloading_chunks(self, tmp_path):
        """Chunks in DOWNLOADING state are reset to PENDING on resume."""
        state = create_test_state(num_chunks=10)
        state.chunks[5].status = ChunkStatus.DOWNLOADING
        
        manager = StateManager(tmp_path / "test.state")
        await manager.save(state)
        
        loaded = await manager.load_for_resume()
        assert loaded.chunks[5].status == ChunkStatus.PENDING
```

### 4.2 File Assembly Tests

```python
# tests/integration/test_file_assembler.py

class TestFileAssembly:
    """Test chunk file concatenation with real filesystem."""
    
    async def test_assemble_correct_order(self, tmp_path):
        """Chunks are assembled in index order."""
        # Create chunks with identifiable content
        chunk_dir = tmp_path / "chunks"
        chunk_dir.mkdir()
        
        for i in range(5):
            (chunk_dir / f"{i:05d}.chunk").write_bytes(f"CHUNK{i}".encode())
        
        output = tmp_path / "output.bin"
        await assemble_file(chunk_dir, output, num_chunks=5)
        
        assert output.read_bytes() == b"CHUNK0CHUNK1CHUNK2CHUNK3CHUNK4"
    
    async def test_assemble_with_hash_verification(self, tmp_path):
        """Assembly verifies each chunk hash."""
        chunk_dir = tmp_path / "chunks"
        chunk_dir.mkdir()
        
        data = os.urandom(1000)
        (chunk_dir / "00000.chunk").write_bytes(data)
        
        expected_hash = hashlib.sha256(data).hexdigest()
        
        # Should succeed
        result = await assemble_file(
            chunk_dir, tmp_path / "output.bin",
            chunk_hashes={0: expected_hash}
        )
        assert result.is_verified
    
    async def test_assemble_detects_corruption(self, tmp_path):
        """Assembly catches corrupted chunk during concatenation."""
        chunk_dir = tmp_path / "chunks"
        chunk_dir.mkdir()
        
        (chunk_dir / "00000.chunk").write_bytes(b"CORRUPTED")
        
        with pytest.raises(ChunkHashMismatchError):
            await assemble_file(
                chunk_dir, tmp_path / "output.bin",
                chunk_hashes={0: "expected_but_wrong_hash"}
            )
```

---

## 5. End-to-End Tests

### 5.1 Full Download Workflow

```python
# tests/e2e/test_full_download.py

class TestFullDownload:
    """End-to-end tests with real HTTP server."""
    
    @pytest.fixture
    async def test_server(self):
        """Start a local HTTP server serving test files."""
        server = TestHTTPServer()
        server.add_file("test.bin", size=10_000_000)  # 10 MB
        await server.start()
        yield server
        await server.stop()
    
    async def test_download_and_verify(self, test_server, tmp_path):
        """Download, verify per-chunk, verify whole-file."""
        cg = ChunkGuard(DownloadConfig(
            chunk_size_bytes=1_000_000,  # 1 MB chunks → 10 chunks
            max_parallel_workers=2,
        ))
        
        result = await cg.download(
            url=test_server.url("test.bin"),
            output_path=tmp_path / "test.bin",
            expected_hash=test_server.hash("test.bin"),
        )
        
        assert result.is_verified
        assert result.total_chunks == 10
        assert (tmp_path / "test.bin").stat().st_size == 10_000_000
    
    async def test_resume_after_interruption(self, test_server, tmp_path):
        """Interrupt download, resume, verify completion."""
        config = DownloadConfig(chunk_size_bytes=1_000_000, max_parallel_workers=1)
        cg = ChunkGuard(config)
        
        # Start download and interrupt after 5 chunks
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(cg.download(
                url=test_server.url("test.bin"),
                output_path=tmp_path / "test.bin",
            ))
            await asyncio.sleep(2)
            task.cancel()
            await task
        
        # Resume
        state_file = tmp_path / ".chunkguard" / "test.bin.state"
        result = await cg.resume(state_file)
        
        assert result.is_verified
    
    async def test_corrupted_chunk_retried(self, test_server, tmp_path):
        """Server returns bad data for one chunk → retry succeeds."""
        # Configure server to corrupt chunk 3 on first attempt
        test_server.corrupt_chunk(file="test.bin", chunk_index=3, attempts=1)
        
        cg = ChunkGuard(DownloadConfig(
            chunk_size_bytes=1_000_000,
            max_retries_per_chunk=3,
        ))
        
        result = await cg.download(
            url=test_server.url("test.bin"),
            output_path=tmp_path / "test.bin",
            expected_hash=test_server.hash("test.bin"),
        )
        
        assert result.is_verified
        assert result.chunks_retried >= 1

    async def test_server_no_range_support(self, test_server, tmp_path):
        """Falls back to single-stream when server doesn't support Range."""
        test_server.disable_range_requests()
        
        cg = ChunkGuard()
        result = await cg.download(
            url=test_server.url("test.bin"),
            output_path=tmp_path / "test.bin",
        )
        
        assert (tmp_path / "test.bin").stat().st_size == 10_000_000
```

---

## 6. Fault Injection Tests

```python
# tests/e2e/test_fault_injection.py

class TestFaultInjection:
    """Test system behavior under various failure conditions."""
    
    async def test_network_drop_mid_chunk(self, test_server, tmp_path):
        """Connection drops in the middle of a chunk download."""
        test_server.drop_connection_after_bytes(
            file="test.bin", chunk_index=2, after_bytes=500_000
        )
        # Should retry and succeed
    
    async def test_server_returns_wrong_size(self, test_server, tmp_path):
        """Server returns fewer bytes than Content-Length claims."""
        test_server.truncate_response(
            file="test.bin", chunk_index=4, truncate_by=1000
        )
        # Should detect size mismatch and retry
    
    async def test_server_very_slow(self, test_server, tmp_path):
        """Server responds extremely slowly (near timeout)."""
        test_server.add_latency(file="test.bin", delay_seconds=250)
        # Should timeout and retry (read_timeout=300)
    
    async def test_disk_full_simulation(self, tmp_path):
        """Disk runs out of space during download."""
        # Use a small tmpfs or mock to simulate ENOSPC
    
    async def test_concurrent_state_access(self, test_server, tmp_path):
        """Multiple workers don't corrupt the state file."""
        # Run download with max workers and verify state consistency
```

---

## 7. Test Execution

### 7.1 Commands

```bash
# Run all tests
pytest

# Run only unit tests (fast)
pytest tests/unit/ -v

# Run integration tests
pytest tests/integration/ -v

# Run E2E tests
pytest tests/e2e/ -v

# Run with coverage
pytest --cov=src --cov-report=html --cov-report=term

# Run specific test
pytest tests/unit/test_chunk_manager.py::TestChunkComputation::test_exact_division -v
```

### 7.2 Coverage Targets

| Module | Line Coverage Target | Branch Coverage Target |
|---|---|---|
| `chunk_manager.py` | 95% | 90% |
| `hash_verifier.py` | 95% | 90% |
| `retry_handler.py` | 95% | 90% |
| `config.py` | 90% | 85% |
| `models.py` | 90% | 85% |
| `exceptions.py` | 80% | 70% |
| `state_manager.py` | 90% | 85% |
| `download_engine.py` | 85% | 80% |
| `file_assembler.py` | 90% | 85% |
| **Overall** | **90%** | **80%** |

### 7.3 CI Pipeline

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.10", "3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: pip install -e ".[dev]"
      - run: pytest --cov=src --cov-report=xml -v
      - uses: codecov/codecov-action@v3
```
