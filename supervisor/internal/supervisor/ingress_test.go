package supervisor

import "testing"

func TestIngressIsExternalToSupervisor(t *testing.T) {
	d := exampleDeployment(t)
	spec, err := ParseSpec(child(d, "spec"))
	if err != nil {
		t.Fatal(err)
	}
	resources := AnalyzerResources(d, spec, Release{}, "hash")
	if len(resources) != 2 || str(resources[0], "kind") != "Deployment" || str(resources[1], "kind") != "Service" {
		t.Fatal("supervisor must manage only the analyzer deployment and service")
	}
	if str(child(resources[1], "spec"), "type") != "ClusterIP" || str(child(resources[1], "metadata"), "name") != "acme-analyzer" {
		t.Fatal("chart ingress requires the stable internal analyzer service")
	}
	for _, endpoint := range []Obj{{}, {"enabled": false}, {"hostname": "pig.example.com", "ingressClassName": "nginx"}} {
		child(d, "spec")["endpoint"] = endpoint
		if _, err := ParseSpec(child(d, "spec")); err == nil {
			t.Fatal("endpoint configuration belongs in chart values, not PIGDeployment")
		}
	}
}
