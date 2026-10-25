// Exercise the same package-loading mode as mgmt 1.0.2 funcgen.
package main

import (
	"log"
	"os"

	"golang.org/x/tools/go/packages"
)

func main() {
	path := os.Args[1]
	cfg := &packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedCompiledGoFiles |
			packages.NeedTypes | packages.NeedTypesInfo | packages.NeedSyntax | packages.NeedModule,
		Env: os.Environ(),
	}
	pkgs, err := packages.Load(cfg, path)
	if err != nil {
		log.Fatal(err)
	}
	if packages.PrintErrors(pkgs) > 0 || len(pkgs) != 1 || pkgs[0].Types == nil || pkgs[0].Types.Scope() == nil {
		log.Fatal("package type loading failed")
	}
	log.Print("standard-library package type loading passed")
}
