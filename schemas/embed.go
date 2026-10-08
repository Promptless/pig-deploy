// Package schemas embeds the public contracts also used by release tooling.
package schemas

import _ "embed"

//go:embed pigdeployment-spec.json
var Deployment []byte

//go:embed release.json
var Release []byte
