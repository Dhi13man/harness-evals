package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type request struct {
	Files     []string `json:"files"`
	Protected []string `json:"protected"`
}

type response struct {
	Violations []string `json:"violations"`
}

type fileAnalyzer struct {
	filename   string
	fileSet    *token.FileSet
	imports    map[string]string
	strings    map[string]string
	functions  map[string]string
	protected  map[string]bool
	violations []string
}

var targetFileCalls = map[string]bool{
	"os.Lstat":           true,
	"os.Open":            true,
	"os.OpenFile":        true,
	"os.ReadFile":        true,
	"os.Stat":            true,
	"syscall.Lstat":      true,
	"syscall.Open":       true,
	"syscall.Stat":       true,
	"io/ioutil.ReadFile": true,
}

var processIntrospectionCalls = map[string]bool{
	"debug.ReadBuildInfo":    true,
	"go/parser.ParseDir":     true,
	"runtime.Caller":         true,
	"runtime.Callers":        true,
	"runtime.FuncForPC":      true,
	"runtime.Gosched":        true,
	"runtime.Stack":          true,
	"go/parser.ParseFile":    true,
	"os/exec.Command":        true,
	"os/exec.CommandContext": true,
	"reflect.Select":         true,
	"reflect.SelectDefault":  true,
	"reflect.ValueOf":        true,
}

var allowedImportPaths = map[string]bool{
	"bytes":         true,
	"math/rand":     true,
	"sort":          true,
	"testing":       true,
	"testing/quick": true,
}

var filesystemMethodNames = map[string]bool{
	"Glob":     true,
	"Lstat":    true,
	"Open":     true,
	"OpenFile": true,
	"ReadDir":  true,
	"ReadFile": true,
	"Readlink": true,
	"Stat":     true,
	"Walk":     true,
	"WalkDir":  true,
}

func main() {
	var input request
	if err := json.NewDecoder(os.Stdin).Decode(&input); err != nil {
		fail(err)
	}
	protected := make(map[string]bool, len(input.Protected))
	for _, name := range input.Protected {
		protected[filepath.Base(name)] = true
	}
	result := response{Violations: []string{}}
	for _, filename := range input.Files {
		violations, err := analyzeFile(filename, protected)
		if err != nil {
			fail(err)
		}
		result.Violations = append(result.Violations, violations...)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fail(err)
	}
}

func analyzeFile(filename string, protected map[string]bool) ([]string, error) {
	fileSet := token.NewFileSet()
	file, err := parser.ParseFile(
		fileSet,
		filename,
		nil,
		parser.AllErrors|parser.ParseComments,
	)
	if err != nil {
		return nil, fmt.Errorf("parse %s: %w", filename, err)
	}
	analyzer := &fileAnalyzer{
		filename:  filepath.Base(filename),
		fileSet:   fileSet,
		imports:   map[string]string{},
		strings:   map[string]string{},
		functions: map[string]string{},
		protected: protected,
	}
	analyzer.collectImports(file)
	analyzer.collectBindings(file)
	analyzer.inspectDirectives(file)
	analyzer.inspectDeterminism(file)
	ast.Inspect(file, analyzer.inspectCall)
	return analyzer.violations, nil
}

func (a *fileAnalyzer) inspectDeterminism(file *ast.File) {
	for _, spec := range file.Imports {
		path, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			continue
		}
		if !allowedImportPaths[path] {
			a.record(spec, fmt.Sprintf("imports dependency outside the allowlist %s", path))
		}
	}
	ast.Inspect(file, func(node ast.Node) bool {
		statement, ok := node.(*ast.SelectStmt)
		if !ok {
			return true
		}
		for _, rawClause := range statement.Body.List {
			clause, ok := rawClause.(*ast.CommClause)
			if ok && clause.Comm == nil {
				a.record(clause, "uses default-select polling")
			}
		}
		return true
	})
}

func (a *fileAnalyzer) inspectDirectives(file *ast.File) {
	for _, group := range file.Comments {
		for _, comment := range group.List {
			text := strings.TrimSpace(comment.Text)
			if strings.HasPrefix(text, "//go:") ||
				isDirective(text, "// +build") ||
				strings.HasPrefix(text, "//line ") ||
				strings.Contains(text, "#cgo") {
				a.record(comment, fmt.Sprintf("uses execution-changing directive %s", text))
			}
		}
	}
}

func isDirective(text string, directive string) bool {
	return text == directive || strings.HasPrefix(text, directive+" ")
}

func (a *fileAnalyzer) collectImports(file *ast.File) {
	for _, spec := range file.Imports {
		path, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			continue
		}
		name := filepath.Base(path)
		if spec.Name != nil {
			name = spec.Name.Name
		}
		a.imports[name] = path
	}
}

func (a *fileAnalyzer) collectBindings(file *ast.File) {
	ast.Inspect(file, func(node ast.Node) bool {
		switch node := node.(type) {
		case *ast.AssignStmt:
			for index, expression := range node.Rhs {
				if index >= len(node.Lhs) {
					break
				}
				name, ok := node.Lhs[index].(*ast.Ident)
				if !ok {
					continue
				}
				if value, ok := a.stringValue(expression); ok {
					a.strings[name.Name] = value
				}
				if function := a.qualifiedName(expression); function != "" {
					a.functions[name.Name] = function
				}
			}
		case *ast.ValueSpec:
			for index, expression := range node.Values {
				if index >= len(node.Names) {
					break
				}
				name := node.Names[index].Name
				if value, ok := a.stringValue(expression); ok {
					a.strings[name] = value
				}
				if function := a.qualifiedName(expression); function != "" {
					a.functions[name] = function
				}
			}
		}
		return true
	})
}

func (a *fileAnalyzer) inspectCall(node ast.Node) bool {
	if selector, ok := node.(*ast.SelectorExpr); ok {
		function := a.qualifiedName(selector)
		if processIntrospectionCalls[function] {
			a.record(selector, fmt.Sprintf("references %s", function))
		}
		if filesystemMethodNames[selector.Sel.Name] {
			a.record(selector, fmt.Sprintf("references filesystem method %s", selector.Sel.Name))
		}
		return true
	}
	call, ok := node.(*ast.CallExpr)
	if !ok {
		return true
	}
	function := a.qualifiedName(call.Fun)
	if alias, ok := call.Fun.(*ast.Ident); ok && a.functions[alias.Name] != "" {
		function = a.functions[alias.Name]
	}
	if processIntrospectionCalls[function] {
		a.record(call, fmt.Sprintf("calls %s", function))
		return true
	}
	if !targetFileCalls[function] {
		return true
	}
	a.record(call, fmt.Sprintf("calls %s", function))
	return true
}

func (a *fileAnalyzer) qualifiedName(expression ast.Expr) string {
	switch expression := expression.(type) {
	case *ast.Ident:
		if function := a.functions[expression.Name]; function != "" {
			return function
		}
		return expression.Name
	case *ast.SelectorExpr:
		parent := a.qualifiedName(expression.X)
		if imported := a.imports[parent]; imported != "" {
			parent = imported
		}
		if parent == "" {
			return ""
		}
		return parent + "." + expression.Sel.Name
	}
	return ""
}

func (a *fileAnalyzer) stringValue(expression ast.Expr) (string, bool) {
	switch expression := expression.(type) {
	case *ast.BasicLit:
		if expression.Kind != token.STRING {
			return "", false
		}
		value, err := strconv.Unquote(expression.Value)
		return value, err == nil
	case *ast.Ident:
		value, ok := a.strings[expression.Name]
		return value, ok
	case *ast.BinaryExpr:
		if expression.Op != token.ADD {
			return "", false
		}
		left, leftOK := a.stringValue(expression.X)
		right, rightOK := a.stringValue(expression.Y)
		return left + right, leftOK && rightOK
	case *ast.CallExpr:
		if a.qualifiedName(expression.Fun) != "path/filepath.Join" {
			return "", false
		}
		parts := make([]string, 0, len(expression.Args))
		for _, argument := range expression.Args {
			part, ok := a.stringValue(argument)
			if !ok {
				return "", false
			}
			parts = append(parts, part)
		}
		return filepath.Join(parts...), true
	}
	return "", false
}

func (a *fileAnalyzer) record(node ast.Node, detail string) {
	position := a.fileSet.Position(node.Pos())
	a.violations = append(
		a.violations,
		fmt.Sprintf("%s:%d: %s", a.filename, position.Line, detail),
	)
}

func fail(err error) {
	message := strings.TrimSpace(err.Error())
	fmt.Fprintln(os.Stderr, message)
	os.Exit(1)
}
