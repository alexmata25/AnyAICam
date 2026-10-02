<?php
declare(strict_types=1);

/**
 * Append website-submission data to a directory outside the public document
 * root. The website checkout is deployed as public_html, so its parent is the
 * first private location available to the same hosting account.
 */
function anyaicam_private_storage_directory(): ?string
{
    $configuredDocumentRoot = (string)($_SERVER['DOCUMENT_ROOT'] ?? '');
    $documentRoot = $configuredDocumentRoot !== '' ? realpath($configuredDocumentRoot) : false;
    // CLI validation has no web server document root; use the script directory
    // only for that non-deployed fallback.
    if ($documentRoot === false && PHP_SAPI === 'cli') {
        $documentRoot = realpath(__DIR__);
    }
    $privateParent = realpath(dirname(__DIR__));
    if ($documentRoot === false || $privateParent === false) {
        return null;
    }

    $directory = $privateParent . DIRECTORY_SEPARATOR . 'anyaicam-private-data';
    if (is_link($directory)) {
        return null;
    }

    // Check before creating anything so a deployment that keeps the source in
    // a document-root subdirectory fails closed without creating a web folder.
    $comparisonRoot = DIRECTORY_SEPARATOR === '\\' ? strtolower($documentRoot) : $documentRoot;
    $comparisonDirectory = DIRECTORY_SEPARATOR === '\\' ? strtolower($directory) : $directory;
    $documentPrefix = rtrim($comparisonRoot, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR;
    $directoryPrefix = rtrim($comparisonDirectory, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR;
    if ($comparisonDirectory === $comparisonRoot || strncmp($directoryPrefix, $documentPrefix, strlen($documentPrefix)) === 0) {
        return null;
    }

    if (!is_dir($directory) && !@mkdir($directory, 0700, true) && !is_dir($directory)) {
        return null;
    }

    $privateRoot = realpath($directory);
    if ($privateRoot === false) {
        return null;
    }

    $documentPrefix = rtrim($documentRoot, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR;
    $privatePrefix = rtrim($privateRoot, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR;
    $comparisonPrivate = DIRECTORY_SEPARATOR === '\\' ? strtolower($privateRoot) : $privateRoot;
    $comparisonPrefix = rtrim($comparisonRoot, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR;
    $comparisonPrivatePrefix = rtrim($comparisonPrivate, DIRECTORY_SEPARATOR) . DIRECTORY_SEPARATOR;
    if ($comparisonPrivate === $comparisonRoot || strncmp($comparisonPrivatePrefix, $comparisonPrefix, strlen($comparisonPrefix)) === 0) {
        return null;
    }

    @chmod($privateRoot, 0700);
    return $privateRoot;
}

/** Append a private website log using a basename-only filename. */
function anyaicam_append_private_log(string $filename, string $contents): bool
{
    if ($filename === '' || basename($filename) !== $filename || !preg_match('/\A[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}\z/D', $filename)) {
        return false;
    }

    $directory = anyaicam_private_storage_directory();
    if ($directory === null) {
        error_log('AnyAiCam website private storage is unavailable; no submission data was persisted.');
        return false;
    }

    $path = $directory . DIRECTORY_SEPARATOR . $filename;
    if (is_link($path)) {
        return false;
    }
    $written = @file_put_contents($path, $contents, FILE_APPEND | LOCK_EX);
    if ($written === false) {
        error_log('AnyAiCam website private log write failed for ' . $filename . '.');
        return false;
    }

    @chmod($path, 0600);
    return true;
}
