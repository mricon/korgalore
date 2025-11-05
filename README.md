# Korgalore

A tool for feeding public-inbox git repositories directly into Gmail as an alternative to subscribing.

WARNING: This is beta-quality software. It can explode or cause you to miss mail.

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

## Documentation

See the docs directory for detailed instructions on installing, configuring, and using.

## Contributing

Send email to tools@kernel.org.

## License

GPLv2.

## Support

Send email to tools@kernel.org.