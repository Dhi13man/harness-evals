package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

const maxSourceBytes = 8 * 1024 * 1024

type request struct {
	Root           string `json:"root"`
	Package        string `json:"package"`
	RequiredSource string `json:"required_source"`
	APIContract    string `json:"api_contract"`
}

type response struct {
	ProductionFiles []string `json:"production_files"`
	TestFiles       []string `json:"test_files"`
	Violations      []string `json:"violations"`
}

var forbiddenBuildExtensions = map[string]struct{}{
	".c": {}, ".cc": {}, ".cpp": {}, ".cxx": {}, ".f": {}, ".f90": {},
	".for": {}, ".h": {}, ".hh": {}, ".hpp": {}, ".m": {}, ".mm": {},
	".s": {}, ".swig": {}, ".swigcxx": {}, ".syso": {},
}

func main() {
	decoder := json.NewDecoder(os.Stdin)
	decoder.DisallowUnknownFields()
	var input request
	if err := decoder.Decode(&input); err != nil {
		fail(fmt.Errorf("decode request: %w", err))
	}
	result, err := inspect(input)
	if err != nil {
		fail(err)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fail(fmt.Errorf("encode response: %w", err))
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, err)
	os.Exit(2)
}

func inspect(input request) (response, error) {
	root, err := filepath.Abs(input.Root)
	if err != nil {
		return response{}, fmt.Errorf("resolve root: %w", err)
	}
	metadata, err := os.Stat(root)
	if err != nil {
		return response{}, fmt.Errorf("stat root: %w", err)
	}
	if !metadata.IsDir() {
		return response{}, fmt.Errorf("root is not a directory: %s", root)
	}

	result := response{
		ProductionFiles: []string{},
		TestFiles:       []string{},
		Violations:      []string{},
	}
	seenRequired := false
	err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		relative = filepath.ToSlash(relative)
		if entry.Type()&os.ModeSymlink != 0 {
			result.Violations = append(result.Violations, relative+": symbolic links are forbidden")
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.IsDir() {
			if entry.Name() == "vendor" {
				result.Violations = append(result.Violations, relative+": vendor directories are forbidden")
				return filepath.SkipDir
			}
			return nil
		}
		if !entry.Type().IsRegular() {
			result.Violations = append(result.Violations, relative+": special files are forbidden")
			return nil
		}

		name := entry.Name()
		extension := strings.ToLower(filepath.Ext(name))
		if name == "go.sum" || name == "go.work" || name == "go.work.sum" {
			result.Violations = append(result.Violations, relative+": external module/workspace state is forbidden")
			return nil
		}
		if name == "go.mod" {
			if relative != "go.mod" {
				result.Violations = append(result.Violations, relative+": nested modules are forbidden")
			}
			return nil
		}
		if _, forbidden := forbiddenBuildExtensions[extension]; forbidden {
			result.Violations = append(result.Violations, relative+": non-Go build input is forbidden")
			return nil
		}
		if extension != ".go" {
			return nil
		}
		if filepath.Dir(relative) != "." {
			result.Violations = append(result.Violations, relative+": nested Go packages are forbidden")
			return nil
		}

		isTest := strings.HasSuffix(name, "_test.go")
		violations, err := inspectGoFile(path, relative, input.Package, isTest)
		if err != nil {
			return err
		}
		result.Violations = append(result.Violations, violations...)
		if isTest {
			result.TestFiles = append(result.TestFiles, relative)
		} else {
			result.ProductionFiles = append(result.ProductionFiles, relative)
			seenRequired = seenRequired || relative == input.RequiredSource
		}
		return nil
	})
	if err != nil {
		return response{}, fmt.Errorf("walk candidate module: %w", err)
	}
	if !seenRequired {
		result.Violations = append(result.Violations, input.RequiredSource+": required production source is missing")
	}
	if len(result.ProductionFiles) == 0 {
		result.Violations = append(result.Violations, "candidate module has no production Go source")
	}
	sort.Strings(result.ProductionFiles)
	sort.Strings(result.TestFiles)
	apiViolations, err := inspectAPI(root, input.Package, input.APIContract, result.ProductionFiles)
	if err != nil {
		return response{}, err
	}
	result.Violations = append(result.Violations, apiViolations...)
	sort.Strings(result.Violations)
	result.Violations = unique(result.Violations)
	return result, nil
}

func inspectAPI(root, packageName, contract string, productionFiles []string) ([]string, error) {
	if contract == "" {
		return nil, nil
	}
	fileSet := token.NewFileSet()
	files := make([]*ast.File, 0, len(productionFiles))
	for _, relative := range productionFiles {
		file, err := parser.ParseFile(fileSet, filepath.Join(root, relative), nil, parser.AllErrors)
		if err != nil {
			return []string{fmt.Sprintf("%s: cannot inspect exported API: %v", relative, err)}, nil
		}
		files = append(files, file)
	}
	configuration := types.Config{Importer: importer.Default()}
	checked, err := configuration.Check(packageName, fileSet, files, nil)
	if err != nil {
		return []string{fmt.Sprintf("exported API could not be type-checked: %v", err)}, nil
	}
	switch contract {
	case "tagrank-v1":
		return inspectTagrankAPI(checked), nil
	case "counterstore-v1":
		return inspectCounterstoreAPI(checked), nil
	default:
		return nil, fmt.Errorf("unknown API contract %q", contract)
	}
}

func exportedScopeNames(scope *types.Scope) []string {
	names := []string{}
	for _, name := range scope.Names() {
		if ast.IsExported(name) {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

func namedType(scope *types.Scope, name string) (*types.Named, bool) {
	object, ok := scope.Lookup(name).(*types.TypeName)
	if !ok {
		return nil, false
	}
	named, ok := object.Type().(*types.Named)
	return named, ok
}

func basicKind(value types.Type, kind types.BasicKind) bool {
	basic, ok := value.Underlying().(*types.Basic)
	return ok && basic.Kind() == kind
}

func exactFunction(scope *types.Scope, name string, parameters []types.BasicKind, resultCheck func(*types.Tuple) bool) bool {
	function, ok := scope.Lookup(name).(*types.Func)
	if !ok {
		return false
	}
	signature, ok := function.Type().(*types.Signature)
	if !ok || signature.Recv() != nil || signature.Variadic() || signature.Params().Len() != len(parameters) {
		return false
	}
	for index, kind := range parameters {
		if !basicKind(signature.Params().At(index).Type(), kind) {
			return false
		}
	}
	return resultCheck(signature.Results())
}

func exactMethod(named *types.Named, name string, parameters []types.BasicKind, resultCheck func(*types.Tuple) bool) bool {
	selection := types.NewMethodSet(types.NewPointer(named)).Lookup(nil, name)
	if selection == nil {
		return false
	}
	signature, ok := selection.Obj().Type().(*types.Signature)
	if !ok || signature.Variadic() || signature.Params().Len() != len(parameters) {
		return false
	}
	for index, kind := range parameters {
		if !basicKind(signature.Params().At(index).Type(), kind) {
			return false
		}
	}
	return resultCheck(signature.Results())
}

func oneBasicResult(kind types.BasicKind) func(*types.Tuple) bool {
	return func(results *types.Tuple) bool {
		return results.Len() == 1 && basicKind(results.At(0).Type(), kind)
	}
}

func inspectTagrankAPI(pkg *types.Package) []string {
	violations := []string{}
	if fmt.Sprint(exportedScopeNames(pkg.Scope())) != "[Entry MostFrequent]" {
		violations = append(violations, fmt.Sprintf("exported package names changed: %v", exportedScopeNames(pkg.Scope())))
	}
	entry, ok := namedType(pkg.Scope(), "Entry")
	if !ok {
		return append(violations, "Entry must remain a named struct type")
	}
	structure, ok := entry.Underlying().(*types.Struct)
	if !ok || structure.NumFields() != 2 || structure.Field(0).Name() != "Value" || !basicKind(structure.Field(0).Type(), types.String) || structure.Field(1).Name() != "Count" || !basicKind(structure.Field(1).Type(), types.Int) {
		violations = append(violations, "Entry must contain exactly Value string and Count int")
	}
	function, ok := pkg.Scope().Lookup("MostFrequent").(*types.Func)
	if !ok {
		violations = append(violations, "MostFrequent must remain an exported function")
		return violations
	}
	signature, ok := function.Type().(*types.Signature)
	valid := ok && !signature.Variadic() && signature.Params().Len() == 2 && signature.Results().Len() == 1
	if valid {
		slice, sliceOK := signature.Params().At(0).Type().Underlying().(*types.Slice)
		resultSlice, resultOK := signature.Results().At(0).Type().(*types.Slice)
		valid = sliceOK && basicKind(slice.Elem(), types.String) && basicKind(signature.Params().At(1).Type(), types.Int) && resultOK && types.Identical(resultSlice.Elem(), entry)
	}
	if !valid {
		violations = append(violations, "MostFrequent must have signature func([]string, int) []Entry")
	}
	return violations
}

func inspectCounterstoreAPI(pkg *types.Package) []string {
	violations := []string{}
	if fmt.Sprint(exportedScopeNames(pkg.Scope())) != "[NewStore Store]" {
		violations = append(violations, fmt.Sprintf("exported package names changed: %v", exportedScopeNames(pkg.Scope())))
	}
	store, ok := namedType(pkg.Scope(), "Store")
	if !ok {
		return append(violations, "Store must remain a named struct type")
	}
	structure, ok := store.Underlying().(*types.Struct)
	if !ok {
		violations = append(violations, "Store must remain a struct type")
	} else {
		for index := 0; index < structure.NumFields(); index++ {
			if structure.Field(index).Exported() {
				violations = append(violations, "Store may not add exported fields")
				break
			}
		}
	}
	newStore, ok := pkg.Scope().Lookup("NewStore").(*types.Func)
	if !ok {
		violations = append(violations, "NewStore must remain an exported function")
	} else {
		signature := newStore.Type().(*types.Signature)
		valid := signature.Params().Len() == 0 && signature.Results().Len() == 1
		var pointer *types.Pointer
		if valid {
			pointer, valid = signature.Results().At(0).Type().(*types.Pointer)
		}
		if !valid || !types.Identical(pointer.Elem(), store) {
			violations = append(violations, "NewStore must have signature func() *Store")
		}
	}
	methodSet := types.NewMethodSet(types.NewPointer(store))
	methodNames := []string{}
	for index := 0; index < methodSet.Len(); index++ {
		if methodSet.At(index).Obj().Exported() {
			methodNames = append(methodNames, methodSet.At(index).Obj().Name())
		}
	}
	sort.Strings(methodNames)
	if fmt.Sprint(methodNames) != "[Get Increment Snapshot Transfer]" {
		violations = append(violations, fmt.Sprintf("Store exported methods changed: %v", methodNames))
	}
	mapResult := func(results *types.Tuple) bool {
		if results.Len() != 1 {
			return false
		}
		mapping, ok := results.At(0).Type().Underlying().(*types.Map)
		return ok && basicKind(mapping.Key(), types.String) && basicKind(mapping.Elem(), types.Int)
	}
	if !exactMethod(store, "Increment", []types.BasicKind{types.String}, oneBasicResult(types.Int)) ||
		!exactMethod(store, "Get", []types.BasicKind{types.String}, oneBasicResult(types.Int)) ||
		!exactMethod(store, "Transfer", []types.BasicKind{types.String, types.String, types.Int}, oneBasicResult(types.Bool)) ||
		!exactMethod(store, "Snapshot", nil, mapResult) {
		violations = append(violations, "Store method signatures must remain unchanged")
	}
	return violations
}

func inspectGoFile(path, relative, expectedPackage string, isTest bool) ([]string, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", relative, err)
	}
	if len(content) > maxSourceBytes {
		return []string{relative + ": source exceeds eight MiB"}, nil
	}
	files := token.NewFileSet()
	parsed, err := parser.ParseFile(files, path, content, parser.ParseComments|parser.AllErrors)
	if err != nil {
		return []string{fmt.Sprintf("%s: invalid Go syntax: %v", relative, err)}, nil
	}

	violations := []string{}
	allowedPackage := parsed.Name.Name == expectedPackage
	if isTest {
		allowedPackage = allowedPackage || parsed.Name.Name == expectedPackage+"_test"
	}
	if !allowedPackage {
		violations = append(violations, fmt.Sprintf("%s: package %s is not allowed", relative, parsed.Name.Name))
	}
	for _, imported := range parsed.Imports {
		path, err := strconv.Unquote(imported.Path.Value)
		if err != nil {
			violations = append(violations, relative+": invalid import path")
			continue
		}
		if path == "C" || path == "embed" || path == "unsafe" {
			violations = append(violations, fmt.Sprintf("%s: import %q is forbidden", relative, path))
		}
	}
	for _, group := range parsed.Comments {
		for _, comment := range group.List {
			text := strings.TrimSpace(comment.Text)
			text = strings.TrimSpace(strings.TrimPrefix(text, "//"))
			text = strings.TrimSpace(strings.TrimPrefix(text, "/*"))
			if strings.HasPrefix(text, "go:") || strings.HasPrefix(text, "+build") || strings.HasPrefix(text, "line ") || strings.Contains(text, "#cgo") {
				line := files.Position(comment.Pos()).Line
				violations = append(violations, fmt.Sprintf("%s:%d: compiler directive is forbidden", relative, line))
			}
		}
	}
	if isTest {
		for _, declaration := range parsed.Decls {
			function, ok := declaration.(*ast.FuncDecl)
			if ok && function.Recv == nil && function.Name.Name == "TestMain" {
				violations = append(violations, relative+": TestMain may not control the external oracle")
			}
		}
	}
	return violations, nil
}

func unique(values []string) []string {
	if len(values) < 2 {
		return values
	}
	result := values[:1]
	for _, value := range values[1:] {
		if value != result[len(result)-1] {
			result = append(result, value)
		}
	}
	return result
}
