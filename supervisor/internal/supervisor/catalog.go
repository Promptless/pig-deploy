package supervisor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"time"
)

const MaxCatalogBytes = 1024 * 1024

type CatalogError struct{ Message string }

func (e *CatalogError) Error() string { return e.Message }

type Catalog struct {
	Client *http.Client
	URL    string
}

func NewCatalog(url string) *Catalog {
	return &Catalog{Client: &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, URL: url}
}

// HTTP failures deliberately exclude paths, query strings, credentials and bodies.
func (c *Catalog) fetch(ctx context.Context, address string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return nil, &CatalogError{"invalid catalog URL"}
	}
	response, err := c.Client.Do(req)
	if err != nil {
		return nil, &CatalogError{"catalog request failed for " + req.URL.Hostname()}
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, &CatalogError{fmt.Sprintf("catalog GET %s returned %d", req.URL.Hostname(), response.StatusCode)}
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, MaxCatalogBytes+1))
	if err != nil {
		return nil, &CatalogError{"catalog response read failed"}
	}
	if len(data) > MaxCatalogBytes {
		return nil, &CatalogError{"release artifact exceeds size limit"}
	}
	return data, nil
}

var artifactURL = regexp.MustCompile(`^https://raw\.githubusercontent\.com/Promptless/pig-deploy/[a-f0-9]{40}/[a-zA-Z0-9/_.-]+$`)
var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

func (c *Catalog) VerifiedBytes(ctx context.Context, a Artifact) ([]byte, error) {
	if !artifactURL.MatchString(a.URL) || !digestPattern.MatchString(a.SHA256) {
		return nil, invalid("artifact")
	}
	data, err := c.fetch(ctx, a.URL)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(data)
	if hex.EncodeToString(sum[:]) != a.SHA256 {
		return nil, &CatalogError{"release artifact checksum mismatch"}
	}
	return data, nil
}
func (c *Catalog) Select(ctx context.Context, pin string) (SelectedRelease, error) {
	var result SelectedRelease
	data, err := c.fetch(ctx, c.URL)
	if err != nil {
		return result, err
	}
	type entry struct {
		URL     string `json:"url"`
		SHA256  string `json:"sha256"`
		Version string `json:"version"`
	}
	var catalog struct {
		SchemaVersion int     `json:"schemaVersion"`
		Releases      []entry `json:"releases"`
	}
	if err := strictJSON(data, &catalog); err != nil {
		return result, invalid("catalog")
	}
	if catalog.SchemaVersion != 1 || catalog.Releases == nil {
		return result, invalid("catalog")
	}
	seen := map[string]bool{}
	var selected *entry
	for i := range catalog.Releases {
		e := &catalog.Releases[i]
		v, err := stableVersion(e.Version)
		if err != nil {
			return result, err
		}
		if !artifactURL.MatchString(e.URL) || !digestPattern.MatchString(e.SHA256) {
			return result, invalid("catalog.releases")
		}
		if seen[e.Version] {
			return result, &CatalogError{"catalog contains duplicate immutable versions"}
		}
		seen[e.Version] = true
		if pin != "" && e.Version != pin {
			continue
		}
		if selected == nil {
			selected = e
		} else {
			previous, _ := stableVersion(selected.Version)
			if v.GreaterThan(previous) {
				selected = e
			}
		}
	}
	if selected == nil {
		return result, &CatalogError{"no published release matches this policy"}
	}
	data, err = c.VerifiedBytes(ctx, Artifact{URL: selected.URL, SHA256: selected.SHA256})
	if err != nil {
		return result, err
	}
	var document Obj
	if err = json.Unmarshal(data, &document); err != nil {
		return result, invalid("release")
	}
	r, err := ParseRelease(document)
	if err != nil {
		return result, err
	}
	if r.Version != selected.Version {
		return result, &CatalogError{"manifest version differs from catalog"}
	}
	return SelectedRelease{r, selected.SHA256}, nil
}
func validCatalogURL(address string) bool {
	u, err := url.Parse(address)
	return err == nil && u.Host != "" && (u.Scheme == "https" || u.Scheme == "http") && u.User == nil
}
