// Zip reading limited to what the upload gate needs: the central directory, plus the first
// bytes of each entry so a renamed native binary is caught by its magic number rather than
// by trusting the extension. No third-party code runs here and nothing is written to disk.

const EOCD_SIGNATURE = 0x06054b50;
const CENTRAL_SIGNATURE = 0x02014b50;
const LOCAL_SIGNATURE = 0x04034b50;
const ZIP64_SENTINEL = 0xffffffff;
const MAX_COMMENT = 0xffff;
const MAX_ENTRIES = 4000;
const MAX_NAME = 240;
const SNIFF_BYTES = 8;

// Extensions the event rejects outright, plus compiled Python, which is not readable source.
const NATIVE_SUFFIXES = [".so", ".pyd", ".dll", ".dylib", ".exe", ".o", ".a", ".lib", ".pyc"];

const NATIVE_MAGIC = [
  [0x7f, 0x45, 0x4c, 0x46], // ELF
  [0x4d, 0x5a], // PE / DOS MZ
  [0xfe, 0xed, 0xfa, 0xce], // Mach-O 32 big endian
  [0xce, 0xfa, 0xed, 0xfe], // Mach-O 32 little endian
  [0xfe, 0xed, 0xfa, 0xcf], // Mach-O 64 big endian
  [0xcf, 0xfa, 0xed, 0xfe], // Mach-O 64 little endian
  [0xca, 0xfe, 0xba, 0xbe], // Mach-O universal, also a Java class
];

class ZipError extends Error {}

function findEndOfCentralDirectory(view) {
  const earliest = Math.max(0, view.byteLength - MAX_COMMENT - 22);
  for (let offset = view.byteLength - 22; offset >= earliest; offset--) {
    if (view.getUint32(offset, true) === EOCD_SIGNATURE) return offset;
  }
  throw new ZipError("Not a zip file, or its directory is unreadable");
}

// Names are compared and stored as posix text; anything that could escape the agent
// directory when it is extracted locally is rejected here rather than at extraction time.
function unsafeName(name) {
  if (!name || name.length > MAX_NAME) return "has a missing or overlong path";
  if (name.startsWith("/") || name.includes("\\")) return "is an absolute or Windows path";
  if (/^[a-zA-Z]:/.test(name)) return "carries a drive letter";
  if (name.split("/").some((part) => part === ".." || part === ".")) return "escapes the agent directory";
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f]/.test(name)) return "contains a control character";
  return null;
}

export function readCentralDirectory(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const eocd = findEndOfCentralDirectory(view);
  const count = view.getUint16(eocd + 10, true);
  const size = view.getUint32(eocd + 12, true);
  const start = view.getUint32(eocd + 16, true);
  if (count === MAX_COMMENT || size === ZIP64_SENTINEL || start === ZIP64_SENTINEL) {
    throw new ZipError("Zip64 archives are not accepted; an agent this large will not upload");
  }
  if (count > MAX_ENTRIES) throw new ZipError(`Too many files: ${count} over the ${MAX_ENTRIES} limit`);
  if (start + size > bytes.byteLength) throw new ZipError("Truncated zip: the directory runs past the end");

  const entries = [];
  let offset = start;
  const decoder = new TextDecoder("utf-8", { fatal: false });
  for (let index = 0; index < count; index++) {
    if (offset + 46 > bytes.byteLength || view.getUint32(offset, true) !== CENTRAL_SIGNATURE) {
      throw new ZipError("Truncated zip: a directory entry is malformed");
    }
    const nameLength = view.getUint16(offset + 28, true);
    const extraLength = view.getUint16(offset + 30, true);
    const commentLength = view.getUint16(offset + 32, true);
    const name = decoder.decode(bytes.subarray(offset + 46, offset + 46 + nameLength));
    entries.push({
      name,
      method: view.getUint16(offset + 10, true),
      compressedSize: view.getUint32(offset + 20, true),
      size: view.getUint32(offset + 24, true),
      externalAttributes: view.getUint32(offset + 38, true),
      localOffset: view.getUint32(offset + 42, true),
      directory: name.endsWith("/"),
    });
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

async function inflateHead(compressed, wanted) {
  const stream = new DecompressionStream("deflate-raw");
  const writer = stream.writable.getWriter();
  // The reader is cancelled as soon as the head arrives, which rejects these; that is expected.
  writer.write(compressed).catch(() => {});
  writer.close().catch(() => {});
  const reader = stream.readable.getReader();
  const head = new Uint8Array(wanted);
  let filled = 0;
  try {
    while (filled < wanted) {
      const { value, done } = await reader.read();
      if (done) break;
      const take = Math.min(wanted - filled, value.length);
      head.set(value.subarray(0, take), filled);
      filled += take;
    }
  } catch {
    // A corrupt deflate stream is reported by the caller as an unreadable entry.
  } finally {
    reader.cancel().catch(() => {});
  }
  return head.subarray(0, filled);
}

async function entryHead(bytes, entry) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const local = entry.localOffset;
  if (local + 30 > bytes.byteLength || view.getUint32(local, true) !== LOCAL_SIGNATURE) return null;
  const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
  const end = Math.min(bytes.byteLength, start + entry.compressedSize);
  if (start >= end) return new Uint8Array(0);
  const data = bytes.subarray(start, end);
  if (entry.method === 0) return data.subarray(0, SNIFF_BYTES);
  if (entry.method === 8) return inflateHead(data, SNIFF_BYTES);
  return null;
}

function looksNative(head) {
  return NATIVE_MAGIC.some((magic) => magic.every((byte, index) => head[index] === byte));
}

// Returns {problems, files, unzippedBytes}. A non-empty problems list means the upload is
// refused; the uploader sees every reason at once instead of one per attempt.
export async function inspect(bytes, maxUnzipped) {
  let entries;
  try {
    entries = readCentralDirectory(bytes);
  } catch (error) {
    return { problems: [error instanceof ZipError ? error.message : "Unreadable zip"], files: [], unzippedBytes: 0 };
  }

  const problems = [];
  const files = [];
  let unzippedBytes = 0;

  for (const entry of entries) {
    const reason = unsafeName(entry.name);
    if (reason) {
      problems.push(`"${entry.name}" ${reason}`);
      continue;
    }
    if (entry.directory) continue;
    if (((entry.externalAttributes >>> 16) & 0o170000) === 0o120000) {
      problems.push(`"${entry.name}" is a symlink; ship the file itself`);
      continue;
    }
    const suffix = NATIVE_SUFFIXES.find((value) => entry.name.toLowerCase().endsWith(value));
    if (suffix) {
      problems.push(`"${entry.name}" is a ${suffix} file; the event takes Python source, not binaries`);
      continue;
    }
    unzippedBytes += entry.size;
    files.push({ name: entry.name, size: entry.size });
  }

  if (!files.some((file) => file.name === "agent.py")) {
    problems.push("No agent.py at the root of the zip; the platform does `import agent`");
  }
  if (unzippedBytes > maxUnzipped) {
    const mb = Math.round(maxUnzipped / 1_000_000);
    problems.push(`${unzippedBytes.toLocaleString()} bytes unzipped is over the ${mb} MB limit`);
  }
  if (problems.length) return { problems, files, unzippedBytes };

  for (const entry of entries) {
    if (entry.directory || entry.size === 0) continue;
    const head = await entryHead(bytes, entry);
    if (head === null) {
      problems.push(`"${entry.name}" uses an unsupported compression method`);
    } else if (looksNative(head)) {
      problems.push(`"${entry.name}" is a native binary despite its name; ship Python source`);
    }
  }
  return { problems, files, unzippedBytes };
}
