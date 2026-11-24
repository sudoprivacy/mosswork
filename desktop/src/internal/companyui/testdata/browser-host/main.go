// Start the desktop's normal company proxy for browser integration tests.
package main

import (
	"fmt"
	"io"
	"os"

	"mosswork.desktop/internal/companyui"
)

func main() {
	bundle, err := os.ReadFile(os.Args[2])
	if err != nil {
		panic(err)
	}
	server, err := companyui.Start(os.Args[1], bundle, os.Args[3])
	if err != nil {
		panic(err)
	}
	defer server.Close()
	fmt.Println(server.Entry("account"))
	_, _ = io.Copy(io.Discard, os.Stdin)
}
