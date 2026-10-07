# Windows Authenticode signing

Customer releases require a currently valid Authenticode code-signing certificate whose enhanced key usage includes Code Signing (`1.3.6.1.5.5.7.3.3`) and whose private key is available through the Windows certificate store. The recommended certificate subject/publisher is the legal entity that publishes AnyAiCam. Use an OV or EV certificate from a Windows-trusted public certificate authority; EV or managed cloud signing may establish reputation faster. Never commit certificate files, private keys, PINs, passwords, or cloud credentials.

Install the Windows SDK SignTool and make the certificate available through its approved hardware token, HSM, or certificate-store provider. Set these process-scoped variables without recording secrets:

```powershell
$env:ANYAICAM_SIGNTOOL_PATH = 'C:\Program Files (x86)\Windows Kits\10\bin\<sdk-version>\x64\signtool.exe'
$env:ANYAICAM_SIGN_CERT_SHA1 = '<40-character certificate thumbprint>'
$env:ANYAICAM_TIMESTAMP_URL = 'https://timestamp.digicert.com'
./installer/windows/build.ps1 -Sign
```

The build registers `AnyAiCamSign` with Inno Setup. `SignTool=AnyAiCamSign` signs the setup executable, while `SignedUninstaller=yes` signs Inno's generated uninstaller. `SignedUninstallerDir` retains the generated signed-uninstaller cache under the ignored `output` directory. SignTool uses SHA-256 file digests and RFC 3161 timestamping with SHA-256 timestamps. The build fails unless every produced setup/cached-uninstaller signature is `Valid` and matches the requested certificate thumbprint.

After installing, verify the deployed uninstaller independently:

```powershell
Get-AuthenticodeSignature 'C:\Program Files\AnyAiCam\unins000.exe' | Format-List Status,StatusMessage,SignerCertificate,TimeStamperCertificate
& $env:ANYAICAM_SIGNTOOL_PATH verify /pa /all /v 'C:\Program Files\AnyAiCam\unins000.exe'
```

For Azure Artifact Signing, authenticate with the approved Azure identity and use the managed signing provider. The metadata file contains only the endpoint, signing-account name, and certificate-profile name; never place access tokens in it or in build logs.

```powershell
$env:ANYAICAM_SIGNTOOL_PATH = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe'
$env:ANYAICAM_AZURE_SIGNING_DLIB = "$env:LOCALAPPDATA\Microsoft\MicrosoftArtifactSigningClientTools\Azure.CodeSigning.Dlib.dll"
$env:ANYAICAM_AZURE_SIGNING_METADATA = 'C:\AnyAiCamSigning\metadata.json'
./installer/windows/build.ps1 -AzureSign
```

This passes `/dlib` and `/dmdf` to SignTool and timestamps through `http://timestamp.acs.microsoft.com`. Inno Setup invokes the same Azure signing command for its generated uninstaller through `SignedUninstaller=yes`, then signs the outer setup executable. The build verifies the setup executable and any retained signed-uninstaller cache entries with both `Get-AuthenticodeSignature` and `signtool verify /pa /all`. After installation, verify the deployed `unins000.exe` with both commands before running the uninstall lifecycle test.

Keep Smart App Control enabled for the signed Dell validation. Verify setup and deployed-uninstaller signatures before testing install, service health, UI, uninstall, Program Files cleanup, ProgramData preservation, and reinstall. Without a legitimate certificate, the signing command and verification gates can be syntax-tested, but Smart App Control trust and signed uninstall cannot be claimed. Run unsigned lifecycle checks only in a disposable Windows VM snapshot where Smart App Control is Off or Evaluation; do not weaken the Dell policy.
