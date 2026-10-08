package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/Promptless/pig-deploy/supervisor/internal/supervisor"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

var version = "dev"

func main() {
	showVersion := flag.Bool("version", false, "print supervisor version")
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}
	ctrl.SetLogger(zap.New())
	config, err := rest.InClusterConfig()
	if err != nil {
		ctrl.Log.Error(fmt.Errorf("service account configuration unavailable"), "Startup failed")
		os.Exit(1)
	}
	err = supervisor.Run(ctrl.SetupSignalHandler(), config, supervisor.Options{Namespace: os.Getenv("WATCH_NAMESPACE"), SystemNamespace: os.Getenv("POD_NAMESPACE"), Holder: os.Getenv("POD_UID"), SupervisorName: os.Getenv("SUPERVISOR_NAME"), CatalogURL: os.Getenv("RELEASE_CATALOG_URL")})
	if err != nil {
		ctrl.Log.Error(err, "Supervisor stopped")
		os.Exit(1)
	}
}
