# The ONE approved publisher of AnyAiCam Windows releases (Codex security review, 2026-10-17).
# A VALID Authenticode signature is not enough: any trusted publisher can produce one. AnyAiCam is signed with Azure
# Trusted Signing (Artifact Signing), whose leaf certificates are short-lived and rotate (thumbprints change daily, the
# issuing CA "Microsoft ID Verified CS EOC CA nn" rotates), so the pin is the publisher IDENTITY:
#   - the exact subject of the validated identity;
#   - the durable per-identity EKU Azure puts on every certificate of that identity;
#   - a chain ending at the Microsoft Identity Verification Root CA 2020 (by thumbprint).
# Read from the approved signed release 1.3.1-7c66928 (anyaicam-release-windows\candidates\1.3.1-7c66928-signed).
$AnyAiCamPublisherSubject = 'CN=Any AI Cam, O=Any AI Cam, L=Porter, S=Texas, C=US'
$AnyAiCamPublisherIdentityEku = '1.3.6.1.4.1.311.97.227715007.494188131.681389833.725589707'
$AnyAiCamPublisherRootThumbprint = 'F40042E2E5F7E8EF8189FED15519AECE42C3BFA2'  # Microsoft Identity Verification Root CA 2020

# The certificates a signed file carries in its own PKCS#7 (Authenticode PE or catalog): leaf, the Azure issuing CA
# ("Microsoft ID Verified CS EOC CA nn"), the PCA and the root. Empty when the file carries none or cannot be read.
function Get-AnyAiCamEmbeddedCertificates([string]$SignedFile) {
    $certificates = New-Object Security.Cryptography.X509Certificates.X509Certificate2Collection
    if ($SignedFile -and (Test-Path -LiteralPath $SignedFile -PathType Leaf)) {
        try { $certificates.Import($SignedFile) } catch { }
    }
    return ,$certificates
}

# "" when the signature is valid AND from the approved publisher; otherwise why not.
# $SignedFile (1.3.2, Sandbox acceptance 2026-10-20): the file the signature came from. Its embedded intermediates are
# given to the chain builder, so the check works OFFLINE on a clean PC. Without them, a machine that had never cached
# the rotating Azure issuing CA built a one-element chain, refused the runtime catalog, and Smart App Control then
# blocked the bundled runtime (setup exit 20). Trust is unchanged: the signature must be Valid (WinVerifyTrust) and
# the chain must end at the pinned root thumbprint.
function Get-AnyAiCamPublisherProblem($Signature, [string]$SignedFile = '') {
    if ($null -eq $Signature -or [string]$Signature.Status -ne 'Valid') {
        return "not validly signed ($(if ($Signature) { $Signature.Status } else { 'no signature' }))"
    }
    $certificate = $Signature.SignerCertificate
    if ($null -eq $certificate) { return 'no signer certificate' }
    if ($certificate.Subject -cne $AnyAiCamPublisherSubject) { return "signed by an unapproved publisher ($($certificate.Subject))" }
    $usages = @($certificate.EnhancedKeyUsageList | ForEach-Object { [string]$_.ObjectId })
    if ($usages -notcontains $AnyAiCamPublisherIdentityEku) { return 'the signing certificate does not carry the approved publisher identity' }
    $chain = New-Object Security.Cryptography.X509Certificates.X509Chain
    # Authenticode already established validity at signing time (timestamp); the short-lived leaf may be expired now,
    # and installs must work offline -- so only the chain's SHAPE is checked here.
    $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    $chain.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::IgnoreNotTimeValid
    $embedded = Get-AnyAiCamEmbeddedCertificates $SignedFile
    if ($embedded.Count) { [void]$chain.ChainPolicy.ExtraStore.AddRange($embedded) }
    [void]$chain.Build($certificate)
    if ($chain.ChainElements.Count -lt 2) { return 'the signing certificate has no issuing chain' }
    $root = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate
    if ($root.Thumbprint -ne $AnyAiCamPublisherRootThumbprint) { return 'the signing certificate does not chain to the approved Microsoft identity-verification root' }
    return ''
}
