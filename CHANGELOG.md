# Changelog

All notable changes to undervoice are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section for a version is what appears on its GitHub release page.

## [Unreleased]

### Added

- `undervoice abx`: a blind ABX listening test, with results saved to CSV, combined across listeners and scored per level, or the clips exported for playing another way.
- `--pause-level` and **In pauses** in the window keep the data going through pauses at a set level below the recent speech, so frames survive pauses on noisy paths. Off by default.
- `--cw-id` and **CW ID every 10 minutes** in the window end an over with your callsign in Morse when 10 minutes have passed since the last ID. Without it the window reminds you to identify every 10 minutes while you transmit.
- `sweep` prints a 95% upper bound on the frame loss rate, and a slow test (`pytest -m slow`, run weekly in CI) shows the under-1% target over 480 frames.
- README sections on design choices, prior art and the listening test.

## [0.4.0] - 2026-11-19

### Added

- The window starts listening as soon as it opens. Untick **Listen whenever undervoice is open** to start it by hand instead; the choice is remembered.

## [0.3.0] - 2026-11-19

### Added

- Install with Scoop: `scoop bucket add londopy https://github.com/Londopy/scoop-bucket`, then `scoop install londopy/undervoice`.
- Hold Space in the window to talk, and let go to stop. Buttons there take Enter instead of Space.
- A fresh frame starts whenever speech comes back after 3 s of silence, so overs keyed by the radio's own PTT or a VOX interface line up with frames.

## [0.2.0] - 2026-11-19

### Added

- `tx --ptt` keys the radio through a CM108-type USB interface's GPIO (DMK URI, AIOC), a serial port's RTS or DTR line (Digirig), or a CAT command through Hamlib's rigctld, with `--txdelay` for the radio to come up to power. In mic mode Enter keys and unkeys, and each transmission starts a fresh frame.
- `undervoice devices` lists every way this machine can key the radio.
- A window (`undervoice gui`, or `undervoice-gui` with no console on Windows) with station details, sound card and PTT pickers, transmit and send-a-recording buttons, an input level meter and a table of frames heard. It follows the Windows light or dark setting and accent color.
- The window remembers your station, sound cards, PTT and levels in `config.toml`.
- Update checks against GitHub Releases, on a schedule and on demand. An update installs only when you click **Download and install**, and only after it matches the release's SHA-256.
- Windows downloads: a per-user installer, an MSI, a portable exe and zip, `install.ps1` and `install.cmd`, with torrents and magnet links for each and `SHA256SUMS.txt` over all of them.
- `undervoice --version`.

### Changed

- Needs Python 3.11 or newer; 3.10 reached its end of life in October 2026.

## [0.1.0] - 2026-11-19

### Added

- Frame format: callsign, grid square and 32-character status in 256 bits with CRC-16, rate 1/2 K=7 convolutional code and interleaver, one frame every 9.3 s.
- Spread-spectrum modulator that shapes the data to the speech and follows its level.
- Receiver with pilot-based acquisition, clock drift correction and sub-band combining.
- Simulated FM channel (emphasis, audio filters, deviation limiter, discriminator noise, repeater hops, sound card clock offset) and a frame-loss sweep.
- Live sound card transmit (mic or WAV file) and receive.
- `rx --record` saves received audio for offline decoding, and `rx` warns when the input clips.
- README sections on testing over the air and on the Part 97 rules.

[Unreleased]: https://github.com/Londopy/undervoice/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Londopy/undervoice/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Londopy/undervoice/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Londopy/undervoice/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Londopy/undervoice/releases/tag/v0.1.0
