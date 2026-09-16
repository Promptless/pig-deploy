// Validate the generated CRD using Kubernetes admission validation without a cluster.
package main

import (
	"context"
	"fmt"
	apiextensions "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions"
	v1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/validation"
	"os"
	"sigs.k8s.io/yaml"
)

func main() {
	data, err := os.ReadFile("../../charts/pig-supervisor/crds/pigdeployments.yaml")
	if err != nil {
		panic(err)
	}
	var external v1.CustomResourceDefinition
	if err = yaml.UnmarshalStrict(data, &external); err != nil {
		panic(err)
	}
	v1.SetDefaults_CustomResourceDefinition(&external)
	var internal apiextensions.CustomResourceDefinition
	if err = v1.Convert_v1_CustomResourceDefinition_To_apiextensions_CustomResourceDefinition(&external, &internal, nil); err != nil {
		panic(err)
	}
	errors := validation.ValidateCustomResourceDefinition(context.Background(), &internal)
	if len(errors) != 0 {
		fmt.Fprintln(os.Stderr, errors.ToAggregate())
		os.Exit(1)
	}
	fmt.Println("Kubernetes CRD admission validation passed")
}
