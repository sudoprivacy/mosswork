package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"time"

	"mosswork.desktop/internal/company"
)

func main() {
	base, directory := os.Args[1], os.Args[2]
	client := company.NewClient(os.Args[3])
	defer client.Close(false)
	input, output := bufio.NewScanner(os.Stdin), json.NewEncoder(os.Stdout)
	for input.Scan() {
		var request struct {
			Method string
			Ticket string
		}
		if json.Unmarshal(input.Bytes(), &request) != nil {
			os.Exit(1)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		var err error
		switch request.Method {
		case "pair":
			platform := "windows"
			if runtime.GOOS == "darwin" {
				platform = "darwin"
			}
			err = client.Pair(ctx, base, request.Ticket, "lease-test", platform)
			if err == nil {
				_, err = client.AddGrant(ctx, directory, true, true)
			}
		case "renew":
			err = client.Renew(ctx, base, request.Ticket)
		case "status":
		default:
			os.Exit(1)
		}
		cancel()
		if err != nil {
			fmt.Fprintln(os.Stderr, "native command failed:", request.Method)
			os.Exit(1)
		}
		if output.Encode(client.Status()) != nil {
			os.Exit(1)
		}
	}
}
