package main

import (
	"github.com/gastownhall/gascity/internal/cityidentity"
	"github.com/gastownhall/gascity/internal/cityinit"
	"github.com/gastownhall/gascity/internal/fsys"
)

// loadIncarnationIDBestEffort reads the city's incarnation id, returning
// "" when the dir has none or it cannot be read. Boot paths ensure the
// file before any runtime or state is built, so "" here means a test
// double over a fake dir — never a booted city.
func loadIncarnationIDBestEffort(cityPath string) string {
	id, err := cityidentity.Load(cityPath)
	if err != nil {
		return ""
	}
	return id
}

func ensureCityScaffold(cityPath string) error {
	return ensureCityScaffoldFS(fsys.OSFS{}, cityPath)
}

func ensureCityScaffoldFS(fs fsys.FS, cityPath string) error {
	return cityinit.EnsureCityScaffoldFS(fs, cityPath)
}

func cityAlreadyInitializedFS(fs fsys.FS, cityPath string) bool {
	return cityinit.CityAlreadyInitializedFS(fs, cityPath)
}

func cityHasScaffoldFS(fs fsys.FS, cityPath string) bool {
	return cityinit.CityHasScaffoldFS(fs, cityPath)
}

func cityCanResumeInitFS(fs fsys.FS, cityPath string) bool {
	return cityinit.CityCanResumeInitFS(fs, cityPath)
}
