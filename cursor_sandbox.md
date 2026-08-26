# Setup of sandboxed development environment for Cursor

## Introduction

Running AI tools with agentic functionality like Cursor on local machines imposes a grave security risk.

Such tools are a primary target of supply chain and prompt attacks, and when not sandboxed properly, impose the risks of leaking confidential data, tokens, and credentials to developer accounts. So, to be able to use such tools without introducing security risks, proper sandboxing measures must be taken.

This article covers one such setup: using a **Dev Container** (which Cursor fully supports via native extension) to isolate the Cursor AI within a specific project folder, blocking its access to your sensitive host system files, keys, and tokens.

Unlike GitHub Copilot for VS Code - where the AI is delivered as an extension installed inside the container - **Cursor's AI is built into the editor itself** and therefore runs on your host machine. However, the Dev Container approach still provides robust protection: Cursor AI can only reference and act on the files that are mounted into the container, so any credentials, SSH keys, or other sensitive files that are not explicitly mounted remain invisible to the AI agent.

Why this is safer than a standard setup:

- **File Access Sandboxing:** Cursor opens the workspace exclusively from inside the container. Files that are not mounted into the container - including SSH keys, browser profiles, credential stores, and other repos on your host - cannot be read, referenced, or leaked by Cursor AI.
- **Network Isolation:** You can further restrict the container's network access via Docker settings to ensure it only communicates with Cursor's AI backend endpoints if needed.
- **No Environment Leakage:** Sensitive environment variables on your Windows host are not passed to the container unless you explicitly map them in the `remoteEnv` section of `devcontainer.json`.
- **Privacy Mode:** Cursor offers a built-in Privacy Mode that prevents your code from being stored or used for model training on Cursor's servers - an additional layer of protection described in Step 3 below.

## Prerequisites

- **Docker Desktop** is installed on your Windows 11 Enterprise host. Get it from here: <https://docs.docker.com/desktop/setup/install/windows-install/>
- **Cursor desktop application** is installed. Get it from: <https://www.cursor.com>
- The **Dev Containers** extension is installed inside Cursor: open the Extensions panel (`Ctrl+Shift+X`), search for **Dev Containers**, and install **anysphere.remote-containers** (published by Anysphere). It might already be installed by default.

## Step-by-step Setup for Folder Isolation

This setup assumes you have all your repository clones in one folder. If your clones are spread across different locations on your machine, you will need to add a separate `mounts` entry in `devcontainer.json` for each repository folder. For convenience, it is recommended to collect all repo folders under a single filesystem location first.

### 1. Configure all the necessary features & Isolation in devcontainer.json

Navigate to your repos folder. Create a subfolder named `".devcontainer"` and inside create an empty file named `"devcontainer.json"`.

Open `.devcontainer\devcontainer.json` in this folder and modify it to:

- Build based on a Dockerfile, which we will create a bit later, instead of running on a stock image.
- Add the required features for development right away. The list below includes a collection of common Python tools, such as PyLint, etc. Feel free to modify where necessary. Python is very useful as agents are pretty efficient with it when making changes, they often use it as a supplementary tool.
- **Do not add additional mounts** for your host home directory or other sensitive locations.

Since **Cursor's AI is built into the editor and does not rely on a separately installed extension**, there is no need to declare any AI extension in the `extensions` list (unlike the Copilot setup, which requires `GitHub.copilot`).

You may still add any other Cursor-compatible extensions needed for development.

Here's the fully-functional `devcontainer.json` example - you can simply copy and use it:

```json
{
    "name": "Isolated DevEnv for Cursor",
    "build": {
        "dockerfile": "Dockerfile",
        "context": "."
    },
    "features": {
        "ghcr.io/devcontainers/features/python:1": {},
        "ghcr.io/wxw-matt/devcontainer-features/command_runner:0": {},
        "ghcr.io/devcontainers-community/features/llvm:3": {},
        "ghcr.io/devcontainers-extra/features/pylint:2": {}
    },
    "containerEnv": {
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt"
    },
    "customizations": {
        // Support for standard VS Code features
        "vscode": {
            "extensions": [
                "ms-python.python",
                "ms-python.vscode-pylance",
                "charliermarsh.ruff"
            ],
            "settings": {
                "python.defaultInterpreterPath": "/usr/local/bin/python"
            }
        },
        // Explicit support for Cursor-specific logic
        "cursor": {
            "extensions": [
                "ms-python.python",
                "ms-python.vscode-pylance",
                "anysphere.remote-containers"
            ]
        }
    },
    "mounts": [],
    "remoteUser": "vscode"
}
```

Now, create **`Dockerfile`** in your `.devcontainer` folder with the following content:

```dockerfile
# Use the base image you selected during setup
FROM mcr.microsoft.com/devcontainers/base:noble

# Install essential development utilities explicitly
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    ca-certificates \
    gnupg2 \
    zstd \
    sudo \
    && rm -rf /var/lib/apt/lists/*

USER root
```

And restart Cursor.

### 2. Open up the folder in the Dev Container

Now, everything should be working smoothly. After restarting Cursor:

- Open the Editor Window in Cursor, then press F1 (or `Ctrl+Shift+P`) and select **Dev Containers: Open Folder in Container** command.
- Select your folder with the repos where you have created `.devcontainer` folder.
- It should now allow you to select the "Isolated DevEnv for Cursor" container for which we have created a config and Dockerfile.
- Cursor will build the Docker image (if it wasn't yet built) and reopen the workspace inside the container. **First start will be slow** (potentially up to 20 minutes) as it downloads image layers and installs features from scratch.
- **Verify Isolation:** Open the terminal inside Cursor (``Ctrl+` ``) and attempt to access your host's sensitive directories (e.g., `ls /mnt/c/Users/YourUser/.ssh`). If configured correctly, the command should fail or return an empty result - the container should only have access to its own virtual filesystem and the explicitly mounted workspace folder, and nowhere else.

### 3. Enable Cursor Privacy Mode

Cursor provides a **Privacy Mode** that prevents your code from being stored on Cursor's servers or used for model training. This is an additional protection layer on top of the Dev Container isolation and should always be enabled when working on proprietary code.

To enable Privacy Mode:

- Open Cursor and go to **Cursor Settings** (top-left Cursor menu → **Settings**, or press `Ctrl+Shift+J`).
- Navigate to the **Privacy** section.
- Enable **Privacy Mode** if it's not yet enabled.

With Privacy Mode on, none of the code Cursor processes will be retained by Cursor, Inc. beyond what is required to serve the immediate AI response.

That's it, now you should be all set for work!
