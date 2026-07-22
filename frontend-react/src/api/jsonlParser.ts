import type {
  ParsedLogSession,
  TranscriptLine,
} from "../types/transcript";

const MAX_FILE_BYTES = 2 * 1024 * 1024;

interface ContentBlock {
  type?: string;
  text?: string;
  name?: string;
  input?: unknown;
  content?: unknown;
  is_error?: boolean;
}

function provenanceFor(fileName: string, lineNumber: number): string {
  return `logs/${fileName}:${lineNumber}`;
}

function lineId(fileName: string, lineNumber: number, suffix = ""): string {
  return `${fileName}:${lineNumber}${suffix}`;
}

function asStringContent(content: unknown): string | null {
  if (typeof content === "string") {
    return content;
  }
  return null;
}

function parseBlocks(content: unknown): ContentBlock[] {
  if (!Array.isArray(content)) {
    return [];
  }
  return content.filter((item) => item && typeof item === "object") as ContentBlock[];
}

function textFromContent(content: unknown): string {
  if (typeof content === "string") {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .map((part) => (typeof part === "string" ? part : ""))
      .filter(Boolean)
      .join("\n");
  }
  return String(content ?? "");
}

function pushLine(
  lines: TranscriptLine[],
  row: Omit<TranscriptLine, "id" | "provenance"> & { suffix?: string },
): void {
  const provenance = provenanceFor(row.fileName, row.lineNumber);
  lines.push({
    id: lineId(row.fileName, row.lineNumber, row.suffix ?? ""),
    fileName: row.fileName,
    lineNumber: row.lineNumber,
    kind: row.kind,
    text: row.text,
    provenance,
    sessionId: row.sessionId,
    timestamp: row.timestamp,
  });
}

function expandUserRow(
  fileName: string,
  lineNumber: number,
  record: Record<string, unknown>,
  message: Record<string, unknown>,
): TranscriptLine[] {
  const sessionId =
    typeof record.sessionId === "string" ? record.sessionId : undefined;
  const timestamp =
    typeof record.timestamp === "string" ? record.timestamp : undefined;
  const isMeta = record.isMeta === true;
  const out: TranscriptLine[] = [];
  const content = message.content;

  const asString = asStringContent(content);
  if (asString !== null) {
    const text = asString.trim();
    if (text) {
      pushLine(out, {
        fileName,
        lineNumber,
        kind: isMeta ? "skipped" : "user_prompt",
        text,
        sessionId,
        timestamp,
      });
    }
    return out;
  }

  for (const [index, block] of parseBlocks(content).entries()) {
    const text = (block.text ?? textFromContent(block.content)).trim();
    if (!text) {
      continue;
    }
    if (block.type === "text") {
      pushLine(out, {
        fileName,
        lineNumber,
        suffix: `:u${index}`,
        kind: isMeta ? "skipped" : "user_prompt",
        text,
        sessionId,
        timestamp,
      });
    } else if (block.type === "tool_result") {
      pushLine(out, {
        fileName,
        lineNumber,
        suffix: `:tr${index}`,
        kind: "tool_result",
        text: text.slice(0, 500),
        sessionId,
        timestamp,
      });
    }
  }

  return out;
}

function expandAssistantRow(
  fileName: string,
  lineNumber: number,
  record: Record<string, unknown>,
  message: Record<string, unknown>,
): TranscriptLine[] {
  const sessionId =
    typeof record.sessionId === "string" ? record.sessionId : undefined;
  const timestamp =
    typeof record.timestamp === "string" ? record.timestamp : undefined;
  const out: TranscriptLine[] = [];
  const blocks = parseBlocks(message.content);

  for (const [index, block] of blocks.entries()) {
    if (block.type === "thinking") {
      continue;
    }
    if (block.type === "text") {
      const text = (block.text ?? "").trim();
      if (text) {
        pushLine(out, {
          fileName,
          lineNumber,
          suffix: `:a${index}`,
          kind: "assistant_text",
          text,
          sessionId,
          timestamp,
        });
      }
    } else if (block.type === "tool_use") {
      const name = block.name ?? "tool";
      pushLine(out, {
        fileName,
        lineNumber,
        suffix: `:tu${index}`,
        kind: "tool_use",
        text: `tool_use: ${name}`,
        sessionId,
        timestamp,
      });
    }
  }

  return out;
}

export function parseJsonlFile(
  fileName: string,
  content: string,
): { lines: TranscriptLine[]; warnings: string[] } {
  const warnings: string[] = [];
  const bytes = new TextEncoder().encode(content).length;
  if (bytes > MAX_FILE_BYTES) {
    warnings.push(
      `${fileName} exceeds 2MB — only the first portion was parsed for the demo.`,
    );
    content = content.slice(0, MAX_FILE_BYTES);
  }

  const lines: TranscriptLine[] = [];
  const rawLines = content.split(/\r?\n/);

  for (let index = 0; index < rawLines.length; index += 1) {
    const lineNumber = index + 1;
    const trimmed = rawLines[index].trim();
    if (!trimmed) {
      continue;
    }

    let record: Record<string, unknown>;
    try {
      record = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      continue;
    }

    const rowType = String(record.type ?? "");
    if (rowType !== "user" && rowType !== "assistant") {
      continue;
    }

    const message =
      record.message && typeof record.message === "object"
        ? (record.message as Record<string, unknown>)
        : null;
    if (!message || !message.content) {
      continue;
    }

    if (rowType === "user") {
      lines.push(...expandUserRow(fileName, lineNumber, record, message));
    } else {
      lines.push(...expandAssistantRow(fileName, lineNumber, record, message));
    }
  }

  return { lines, warnings };
}

export function parseJsonlFiles(
  files: Array<{ name: string; content: string }>,
): ParsedLogSession {
  const allLines: TranscriptLine[] = [];
  const warnings: string[] = [];
  const fileMeta: ParsedLogSession["files"] = [];

  for (const file of files) {
    const parsed = parseJsonlFile(file.name, file.content);
    allLines.push(...parsed.lines);
    warnings.push(...parsed.warnings);
    fileMeta.push({ name: file.name, lineCount: parsed.lines.length });
  }

  return {
    files: fileMeta,
    lines: allLines,
    source: "local-upload",
    warnings: warnings.length > 0 ? warnings : undefined,
  };
}

export async function readFilesAsLogInputs(
  fileList: FileList | File[],
): Promise<Array<{ name: string; content: string }>> {
  const files = Array.from(fileList);
  return Promise.all(
    files.map(async (file) => ({ name: file.name, content: await file.text() })),
  );
}
