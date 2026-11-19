# Changelog

All notable changes to undervoice are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section for a version is what appears on its GitHub release page.

## [Unreleased]

### Added

- `tx --ptt` keys the radio through a CM108-type USB interface (DMK URI, AIOC and similar), with `--ptt-gpio` and `--txdelay`. In mic mode Enter keys and unkeys, and each transmission starts a fresh frame.
- `undervoice devices` lists the interfaces that can key PTT.

## [0.1.0] - 2026-11-19

### Added

- Frame format: callsign, grid square and 32-character status in 256 bits with CRC-16, rate 1/2 K=7 convolutional code and interleaver, one frame every 9.3 s.
- Spread-spectrum modulator that shapes the data to the speech and follows its level.
- Receiver with pilot-based acquisition, clock drift correction and sub-band combining.
- Simulated FM channel (emphasis, audio filters, deviation limiter, discriminator noise, repeater hops, sound card clock offset) and a frame-loss sweep.
- Live sound card transmit (mic or WAV file) and receive.
- `rx --record` saves received audio for offline decoding, and `rx` warns when the input clips.
- README sections on testing over the air and on the Part 97 rules.

[Unreleased]: https://github.com/Londopy/undervoice/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Londopy/undervoice/releases/tag/v0.1.0
