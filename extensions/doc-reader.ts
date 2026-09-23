import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { spawn } from "node:child_process";
import * as path from "node:path";
import * as fs from "node:fs";
import { fileURLToPath } from "node:url";

const CURRENT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DOC_PARSER_SCRIPT = path.resolve(CURRENT_DIR, "..", "scripts", "doc_parser.py");

function runParser(args: string[]): Promise<{ stdout: string; stderr: string; code: number }> {
	return new Promise((resolve) => {
		const proc = spawn("python3", [DOC_PARSER_SCRIPT, ...args]);
		let stdout = "";
		let stderr = "";
		proc.stdout.on("data", (data) => {
			stdout += data.toString();
		});
		proc.stderr.on("data", (data) => {
			stderr += data.toString();
		});
		proc.on("close", (code) => {
			resolve({ stdout, stderr, code: code ?? 0 });
		});
		proc.on("error", (err) => {
			resolve({ stdout: "", stderr: err.message, code: 1 });
		});
	});
}

function resolveDocPath(cwd: string, filePath: string): string | null {
	const direct = path.isAbsolute(filePath) ? filePath : path.resolve(cwd, filePath);
	if (fs.existsSync(direct)) return direct;

	// Search recursively in subdirectories (up to depth 4) to support subject folders
	const targetBase = path.basename(filePath).toLowerCase();
	function search(dir: string, depth: number): string | null {
		if (depth > 4) return null;
		try {
			const entries = fs.readdirSync(dir, { withFileTypes: true });
			for (const entry of entries) {
				if (entry.name.startsWith(".") || entry.name === "node_modules" || entry.name === "viz") continue;
				const full = path.join(dir, entry.name);
				if (entry.isFile() && entry.name.toLowerCase() === targetBase) {
					return full;
				}
				if (entry.isDirectory()) {
					const res = search(full, depth + 1);
					if (res) return res;
				}
			}
		} catch {
			return null;
		}
		return null;
	}

	return search(cwd, 0);
}

export default function docReader(pi: ExtensionAPI) {
	// 1. inspect_doc: Get metadata and table of contents
	pi.registerTool({
		name: "inspect_doc",
		label: "Inspect Document (PDF/EPUB)",
		description:
			"Inspect a local PDF or EPUB file to view its metadata, page count, and Table of Contents / chapter list. Automatically searches in subject subdirectories if only the filename is given. ALWAYS call this first before reading large books or course slides, to identify the exact page range or chapter you need without blowing up the context window.",
		promptSnippet: "Use inspect_doc to view the table of contents and structure of a PDF or EPUB file.",
		parameters: Type.Object({
			filePath: Type.String({
				description: "Path to the .pdf or .epub file (relative, absolute, or filename in a subject folder).",
			}),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const resolvedPath = resolveDocPath(ctx.cwd, params.filePath);

			if (!resolvedPath) {
				return {
					content: [{ type: "text", text: `File not found: '${params.filePath}' (searched in workspace and subject subfolders).` }],
					details: { ok: false },
				};
			}

			const res = await runParser(["toc", resolvedPath]);
			if (res.code !== 0) {
				return {
					content: [{ type: "text", text: `Error inspecting document:\n${res.stderr || res.stdout}` }],
					details: { ok: false },
				};
			}

			return {
				content: [{ type: "text", text: res.stdout }],
				details: { ok: true, path: resolvedPath },
			};
		},
	});

	// 2. read_doc_section: Read a targeted slice of a PDF or EPUB
	pi.registerTool({
		name: "read_doc_section",
		label: "Read Document Section",
		description:
			"Surgically read a specific section of a local study document. For PDFs, specify page range (e.g. pages: '15-28'). For EPUBs, specify section index or identifier (e.g. section: '3'). This preserves token economy and keeps conversations fast and sharp.",
		promptSnippet: "Use read_doc_section to extract specific pages from a PDF or a specific chapter from an EPUB.",
		parameters: Type.Object({
			filePath: Type.String({
				description: "Path to the .pdf or .epub file.",
			}),
			pages: Type.Optional(
				Type.String({
					description: "For PDFs: Page range to extract, e.g. '1-10' or '45-52'.",
				}),
			),
			section: Type.Optional(
				Type.String({
					description: "For EPUBs: Section/Chapter index (0-based) or chapter id/file from inspect_doc.",
				}),
			),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const resolvedPath = resolveDocPath(ctx.cwd, params.filePath);

			if (!resolvedPath) {
				return {
					content: [{ type: "text", text: `File not found: '${params.filePath}' (searched in workspace and subject subfolders).` }],
					details: { ok: false },
				};
			}

			const ext = path.extname(resolvedPath).toLowerCase();
			const args = ["read", resolvedPath];

			if (ext === ".pdf") {
				if (!params.pages) {
					return {
						content: [
							{
								type: "text",
								text: "For PDF files, `pages` parameter is required (e.g. `pages: '12-25'`). Call `inspect_doc` first if unsure.",
							},
						],
						details: { ok: false },
					};
				}
				args.push("--pages", params.pages);
			} else if (ext === ".epub") {
				if (!params.section) {
					return {
						content: [
							{
								type: "text",
								text: "For EPUB files, `section` parameter is required (e.g. `section: '2'`). Call `inspect_doc` to see chapter numbers.",
							},
						],
						details: { ok: false },
					};
				}
				args.push("--section", params.section);
			} else {
				return {
					content: [{ type: "text", text: `Unsupported format '${ext}'. Only .pdf and .epub are supported.` }],
					details: { ok: false },
				};
			}

			const res = await runParser(args);
			if (res.code !== 0) {
				return {
					content: [{ type: "text", text: `Error extracting section:\n${res.stderr || res.stdout}` }],
					details: { ok: false },
				};
			}

			return {
				content: [{ type: "text", text: res.stdout }],
				details: { ok: true, path: resolvedPath },
			};
		},
	});

	// 3. search_doc: Search within a PDF or EPUB
	pi.registerTool({
		name: "search_doc",
		label: "Search in Document",
		description:
			"Search for keywords, concepts, theorems, or formulas inside a local PDF or EPUB. Automatically searches in subject subdirectories if only filename is given. Returns matching page numbers (for PDFs) or chapter sections (for EPUBs) along with context snippets.",
		promptSnippet: "Use search_doc to find where a theorem, formula, or concept appears in your book or course PDF.",
		parameters: Type.Object({
			filePath: Type.String({
				description: "Path to the .pdf or .epub file (relative, absolute, or filename in subject folder).",
			}),
			query: Type.String({
				description: "Term, theorem name, formula, or phrase to search for.",
			}),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const resolvedPath = resolveDocPath(ctx.cwd, params.filePath);

			if (!resolvedPath) {
				return {
					content: [{ type: "text", text: `File not found: '${params.filePath}' (searched in workspace and subject subfolders).` }],
					details: { ok: false },
				};
			}

			const res = await runParser(["search", resolvedPath, params.query]);
			if (res.code !== 0) {
				return {
					content: [{ type: "text", text: `Error searching document:\n${res.stderr || res.stdout}` }],
					details: { ok: false },
				};
			}

			return {
				content: [{ type: "text", text: res.stdout }],
				details: { ok: true, path: resolvedPath },
			};
		},
	});
}
