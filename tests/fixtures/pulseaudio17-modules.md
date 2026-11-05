# PulseAudio 17 module-list contract

The adjacent JSON and TSV fixtures contain the `module-suspend-on-idle` entry
captured from `pactl -f json list modules` and `pactl list short modules` on a
native PulseAudio 17.0 virtual server. Both client and server came from Debian
`pulseaudio-utils` / `pulseaudio` version `17.0+dfsg1-2+b1` (x86_64). The server
loaded only native/CLI Unix protocols, a null sink and the idle module; it did
not load hardware modules or open audio hardware.

PulseAudio 17's JSON module entries have no `index`. The short text listing
provides the module ID, name and arguments separated by tabs. An empty argument
field loses its trailing tabs when the command runner strips whitespace. The
TSV fixture includes that normalization, matching the installer's command runner.
The ID is assigned at runtime and must not be hard-coded.

This format is also defined by `get_module_info_callback()` in
[`src/utils/pactl.c` at v17.0](https://github.com/pulseaudio/pulseaudio/blob/v17.0/src/utils/pactl.c).
Endpoint and stream JSON entries do contain their indices; their fixtures
should not inherit the module schema.
