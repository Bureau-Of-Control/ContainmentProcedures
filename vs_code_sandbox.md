# Setup of sandboxed development environment for VS Code with Copilot extension

## Introduction

Running AI tools with agentic functionality like Cursor or Copilot for VS Code on local machines imposes a grave security risk.

Such tools are a primary target of supply chain and prompt attacks, and when not sandboxed properly, impose the risks of leaking confidential data, tokens, and credentials to developer accounts. So, to be able to use such tools without introducing security risks, proper sandboxing measures must be taken.

This article covers one such setup, setting up a VS Code Dev Container environment that provides a robust way to isolate GitHub Copilot within a specific project folder while blocking its access to your sensitive host system files, keys, and tokens.

Why this is safer than a standard setup:

- **Extension Sandboxing:** The Copilot extension runs inside the container, not on your host OS. It can only "see" files that are mounted into the container.
- **Network Isolation:** You can further restrict the container's network access via Docker settings to ensure it only talks to GitHub's AI endpoints if needed.
- **No Environment Leakage:** Sensitive environment variables on your Windows host are not passed to the container unless you explicitly map them in the remoteEnv section of the JSON.

## Prerequisites

- Docker Desktop is installed on your Windows 11 host. Get it from here: <https://docs.docker.com/desktop/setup/install/windows-install/>
- VS Code is installed with Microsoft's "Dev Containers" extension: <https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers>

## Step-by-step Setup for Folder Isolation

This setup is done with the assumption that you have all the repos in one folder. In case you have clones of repositories all around your machine, you will need to modify devcontainer.json below to add mounts for every single repository clone separately, so for convenience, it might be better to collect all the repo folders in the same filesystem location first.

### 1. Initialize the Configuration and edit the config files

- Open VS Code and navigate to the folder with the repository clones you want to work on.
- Press F1 (or Ctrl+Shift+P) and select **Dev Containers: Add Dev Container Configuration Files....**
- Choose a base image definition (e.g., Python 3, Node.js) that matches your project's needs. In our case, just a common Ubuntu image fits nicely (id: <http://mcr.microsoft.com/devcontainers/base:noble>)

### 2. Configure all the necessary features, Copilot & Isolation in devcontainer.json

VS Code will create a **.devcontainer** folder in `%USERPROFILE%\AppData\Roaming\Code\User\globalStorage\ms-vscode-remote.remote-containers\configs\<your_repository_folder_name>\.devcontainer\`

Open .devcontainer\devcontainer.json in this folder and modify it to:

- Build based on Dockerfile, which we will create a bit later here, instead of running on a stock image (required to be able to spin up the container on a system that has Netskope installed).
- Add the required features for development right away. The list below includes a collection of common C++ tools, Python, PyLint, and LLVM. Feel free to modify where necessary, no sweat if you won't add something right away - you will always be able to add it later on the existing container.
- Add the Copilot extension right away

Here's the fully-functional devcontainer.json example with all the necessary changes, you can simply copy and use it:

```json
{
	"name": "Isolated DevEnv for Copilot",
	// Or use a Dockerfile or Docker Compose file. More info: https://containers.dev/guide/dockerfile
	"build": {
		"dockerfile": "Dockerfile",
		"context": "."
	},
	"features": {
		"ghcr.io/devcontainers/features/python:1": {},
		"ghcr.io/wxw-matt/devcontainer-features/command_runner:0": {},
		"ghcr.io/devcontainers-community/features/llvm:3": {},
		"ghcr.io/devcontainers-extra/features/pylint:2": {},
		"ghcr.io/devcontainer-community/devcontainer-features/collection-c-cpp:1": {}
	},
	"customizations": {
		"vscode": {
			"extensions": [
				"GitHub.copilot",      // Automatically installs Copilot in the container
				"GitHub.copilot-chat" // Optional: Installs Copilot Chat
			],
			"settings": {
				"github.copilot.editor.enableAutoCompletions": true
			}
		}
	},
	// CRITICAL: Ensure NO host folders are mounted except the workspace
	"mounts": [
		// By default, VS Code mounts ONLY the workspace folder so you don't need to add anything here
 		// if you have all the repos in one folder. If not, you will need to add links to your repo folders here for mounting.
		// Do NOT add additional mounts like "${localEnv:USERPROFILE}:/home/node/host"
	]

	// Use 'forwardPorts' to make a list of ports inside the container available locally.
	// "forwardPorts": [],

	// Use 'postCreateCommand' to run commands after the container is created.
	// "postCreateCommand": "uname -a",

	// Uncomment to connect as root instead. More info: https://aka.ms/dev-containers-non-root.
	// "remoteUser": "root"
}
```

After that, create **Dockerfile** in your .devcontainer folder with the following content:

```dockerfile
# Use the image you were previously using
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

### 3. Launch the Isolated Environment

- Navigate to the folder with the repository clones you want to work on.
- Press F1 (or Ctrl+Shift+P) and select **Dev Containers: Reopen in Container**.
- VS Code will build the image and restart inside the container. First start will be slow (might take up to 20 minutes) as it will instantiate the container from scratch, and for this, it will download some Docker image layers.
- **Verify Isolation:** Open the terminal inside VS Code and try to access your host's sensitive directories (e.g., ls /mnt/c/Users/YourUser/.ssh). If configured correctly, the container should only have access to its own virtual filesystem and the specific workspace folder and nowhere else.

**WARNING** 

Important note on the VS Code Copilot automatic conversation compaction feature: as of Aug 29, 2026 this feature is BROKEN. It's overly aggressive, and when token context window fills up to roughly 60%, it starts agressively compacting conversation ON EVERY SINGLE STEP. Compaction is complex process which takes a lot of time (might be up 10-15 minutes sometimes) and useful context is often lost after it, so such an enforced compaction makes it almost impossible to work with LLM (because after every compaction, the context is getting changed and LLM has to reconsider it) and is wasting a ton of computational capacity. Also, Ollama manages context automatically itself and does it pretty well, so VS Code Copilot automatic conversation compaction in this case is both useless and harmful. So it makes total sense to disable it while you're at it. You will still retain the ability to compact coversation yourself via button in Copilot chat or using `/compact` chat command.

To disable this thing, open VS Code command palette via `Ctrl+Shift+P` and search for `Chat: Chat Settings`, and in this settings, search for `summarizeAgentConversationHistory` then uncheck it. Also makes sense then to click on the gearbox icon on the left of this settings and select "Apply to all profiles".
