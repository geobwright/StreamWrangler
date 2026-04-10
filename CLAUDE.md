# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IPTVEditor is a tool for editing IPTV playlists (M3U/M3U8 files) and managing channel data, EPG (Electronic Program Guide) sources, and stream metadata.

## Commands

> Update these once the project is initialized.

```bash
# Install dependencies
# npm install  /  pip install -r requirements.txt  /  go mod tidy

# Run the application
# npm start  /  python main.py  /  go run .

# Run tests
# npm test  /  pytest  /  go test ./...

# Build
# npm run build  /  go build -o iptveditor .
```

## Architecture

> Fill in once the tech stack is chosen.

Key concepts for this domain:
- **M3U/M3U8**: Playlist format where each entry has `#EXTINF` metadata (channel name, logo, group, tvg-id) followed by a stream URL
- **EPG**: XML-based program guide data (XMLTV format), linked to channels via `tvg-id`
- **Groups/Categories**: Channels are organized into groups via the `group-title` attribute
