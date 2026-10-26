//go:build !production || darwin || company

package main

// Development builds look for mosswork-<plat>.zip or mosswork-portable-<plat>-*.zip
// beside the executable. macOS production copies the zip into Resources as
// mosswork-<plat>.zip.
var embeddedPortable []byte
