# Windows internal-test Azure signing path

This is a separate, default-off build mode for isolated acceptance only. It does not replace or relax the production
trust-registry check. The existing `-Sign` and `-AzureSign` modes continue to require a production registry. The new
`-InternalTestAzureSign` mode accepts only a registry whose environment is `synthetic`, writes into a fresh
`output-internal-test` directory, embeds an internal-test marker, adds a conspicuous filename and executable metadata
label, and refuses to reuse an existing internal output directory.

The internal mode uses the same configured Azure Artifact Signing Dlib, metadata, timestamp server, publisher pin,
catalog coverage checks, and Inno Setup signing callback as the Azure production mode. It signs the runtime catalog,
setup, and generated signed uninstaller in the guarded build flow. It does not perform any post-sign editing. Signing
is not authorized by this implementation or its unit tests; an independent source review and separate owner approval
are required before invoking the mode.

Internal artifacts are not customer releases. Do not distribute, publish, or install them in production. The
`internal-test-marker.txt` payload is included in the installation only when the explicit compile-time internal-test
define is present.
