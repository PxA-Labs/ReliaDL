# AI Agents & Memory Management — ChunkGuard

To enable long-term memory, personalization, and context retention for AI agents working on **ChunkGuard**, we utilize the **Mem0** memory system. This document provides developer and agent-level instructions on configuring and using Mem0 memory within the project.

---

## 🚀 Overview of Mem0 Memory

Mem0 provides an intelligent, self-improving memory layer for AI agents. It stores user preferences, system behaviors, and interaction history, and automatically extracts new facts from context.

### Memory Operations

*   **Long-Term Context**: Persists across sessions and platform runs.
*   **Fact Extraction**: Automatically filters out conversational fluff and extracts structured facts.
*   **Semantic Search**: Retrieves contextually relevant facts based on similarity search.

---

## 🔑 Configuration & API Keys

To connect to the Mem0 Platform, you need a Mem0 API key.

### Setting the API Key

Store your API key in your environment variables:

```bash
export MEM0_API_KEY="m0-kfy7muuvGNkeU0BFkVV2fYu6fv65UxA34nJvZ4TC"
```

If you are using LLMs or embedders through other providers (e.g. OpenAI), also set their respective keys:

```bash
export OPENAI_API_KEY="your_openai_api_key_here"
```

---

## 💻 Python Usage Examples

### 1. Connecting to the Mem0 Platform (Recommended)

The standard platform integration uses `MemoryClient` to store and query memories hosted in the Mem0 cloud.

```python
import os
from mem0 import MemoryClient

# Initialize the client (auto-detects MEM0_API_KEY from environment)
client = MemoryClient()

# Alternative: Pass the API key explicitly
# client = MemoryClient(api_key="m0-kfy7muuvGNkeU0BFkVV2fYu6fv65UxA34nJvZ4TC")

# Define user context
USER_ID = "developer_alpha"

# --- Add Data to Memory ---
interaction = [
    {"role": "user", "content": "I prefer 8 workers and 16MB chunk sizes for downloads on stable connections."},
    {"role": "assistant", "content": "Understood. I will remember your configuration preferences."}
]

client.add(interaction, user_id=USER_ID)
print("✅ Context added to Mem0 memory.")

# --- Retrieve / Search Memory ---
query = "What chunk size and worker configuration does the user prefer?"
memories = client.search(query, user_id=USER_ID)

for mem in memories:
    print(f"Fact: {mem['memory']} (ID: {mem['id']})")
```

### 2. Open-Source Local Memory (Optional)

If you prefer to run Mem0 locally without the cloud hosting API, use the `Memory` class config:

```python
from mem0 import Memory

config = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "host": "localhost",
            "port": 6333,
        }
    },
    "llm": {
        "provider": "openai",
        "config": {
            "api_key": os.environ.get("OPENAI_API_KEY")
        }
    }
}

memory = Memory.from_config(config)
memory.add("Prefers chunk size 16MB", user_id="user_1")
```

---

## 🛡️ Best Practices for AI Agents

1. **Clean Inputs**: When adding to memory, send structured messages (roles: `user`/`assistant`/`system`) rather than raw raw logs, to enable clean fact extraction.
2. **Key Rotation**: Never hardcode API keys in code or commit them to the repository. Always use environment variables or secret vaults.
3. **Session Filtering**: Use `user_id` or `session_id` tags to partition memories and prevent cross-user data leakage.
