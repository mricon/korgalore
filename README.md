# Korgalore

A tool for feeding public-inbox git repositories directly into Gmail as an alternative to subscribing.

WARNING: This is alpha-quality software. It will likely explode or cause you to miss mail.

## Overview

Gmail is notoriously hostile to high-volume technical mailing list traffic. It will routinely throttle incoming messages, mark them as spam, or just reject them outright if it doesn't like something about them. Gmail is responsible for hundreds of thousands of messages sitting in the mail queue just waiting to be delivered.

This fairly simple tool will take public-inbox mailboxes and feed them directly into Gmail using their native API.

## Name

It's a play on "k.org lore" and "Orgalorg," who is a primordial cosmic entity in the Adventure Time universe -- the "breaker of worlds," which is basically what Gmail is to mailing lists.

## Features

- Direct integration with public-inbox repositories
- Direct Gmail API integration

## Non-features

- No filtering (use lei for that)
- No querying (use lei for that)

## Installation

### Prerequisites

- Python 3.11 or above
- Git
- Gmail API credentials

### Install

TBD.

## Configuration

### Gmail Setup

Gmail went out of their way to make it super difficult to access your inbox via an API, so please be prepared to suffer a bit. So, you will need to download an OAuth 2.0 Client ID file from Google that will authorize your access.

The best is to follow the "quickstart app" instructions from Google itself:
https://developers.google.com/workspace/gmail/api/quickstart/python#set-up-environment

Choosing "Internal Use" and "Desktop Application" should be the simplest.

Eventually, you should have a "Download JSON" link. Use that to download `client_secret_mumble-long-string.json`. Rename it into `credentials.json` and put into `~/.config/korgalorg/credentials.json`.

After that, run `kgl -v auth` and follow the link to authorize access.

### Config file

Copy `korgalorg-example.toml` into `~/.config/korgalorg/korgalorg.toml`. You can add any lists on lore.kernel.org following the example provided.

TBD: combining with lei.

## Usage

### Basic Usage

For now, just:

`kgl auth`: to authenticate with gmail
`kgl pull`: to pull the configured lists and import them into your gmail

A lot more functionality will be added soon, including a more native background mode.

## Contributing

Send email to tools@kernel.org.

## License

GPLv2.

## Support

Send email to tools@kernel.org.