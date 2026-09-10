"use strict";
// Builds the submission zip in the browser when someone drops a folder or loose files, so
// the upload always has the same shape as `make zip`. Timestamps are fixed, which makes the
// archive a pure function of its contents: re-zipping an unchanged agent uploads as a no-op.

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index++) {
    let value = index;
    for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
    table[index] = value;
  }
  return table;
})();

function crc32(bytes) {
  let crc = 0xffffffff;
  for (let index = 0; index < bytes.length; index++) {
    crc = CRC_TABLE[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

async function deflateRaw(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

// 1980-01-01, the zero point of the DOS date format.
const DOS_TIME = 0;
const DOS_DATE = 33;
const UTF8_FLAG = 0x0800;

export async function buildZip(files) {
  const encoder = new TextEncoder();
  const parts = [];
  const directory = [];
  let offset = 0;

  for (const file of files) {
    const name = encoder.encode(file.name);
    const raw = new Uint8Array(await file.blob.arrayBuffer());
    const crc = crc32(raw);
    const packed = raw.length ? await deflateRaw(raw) : new Uint8Array(0);
    // Storing beats deflating on already-compressed weights; take whichever is smaller.
    const deflated = packed.length < raw.length;
    const body = deflated ? packed : raw;
    const method = deflated ? 8 : 0;

    const header = new DataView(new ArrayBuffer(30));
    header.setUint32(0, 0x04034b50, true);
    header.setUint16(4, 20, true);
    header.setUint16(6, UTF8_FLAG, true);
    header.setUint16(8, method, true);
    header.setUint16(10, DOS_TIME, true);
    header.setUint16(12, DOS_DATE, true);
    header.setUint32(14, crc, true);
    header.setUint32(18, body.length, true);
    header.setUint32(22, raw.length, true);
    header.setUint16(26, name.length, true);
    header.setUint16(28, 0, true);
    parts.push(new Uint8Array(header.buffer), name, body);

    const entry = new DataView(new ArrayBuffer(46));
    entry.setUint32(0, 0x02014b50, true);
    entry.setUint16(4, 20, true);
    entry.setUint16(6, 20, true);
    entry.setUint16(8, UTF8_FLAG, true);
    entry.setUint16(10, method, true);
    entry.setUint16(12, DOS_TIME, true);
    entry.setUint16(14, DOS_DATE, true);
    entry.setUint32(16, crc, true);
    entry.setUint32(20, body.length, true);
    entry.setUint32(24, raw.length, true);
    entry.setUint16(28, name.length, true);
    entry.setUint32(38, 0o100644 << 16, true);
    entry.setUint32(42, offset, true);
    directory.push(new Uint8Array(entry.buffer), name);

    offset += 30 + name.length + body.length;
  }

  const directorySize = directory.reduce((total, chunk) => total + chunk.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, directorySize, true);
  end.setUint32(16, offset, true);

  return new Blob([...parts, ...directory, new Uint8Array(end.buffer)], { type: "application/zip" });
}
