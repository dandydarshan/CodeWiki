import mermaid from "mermaid";

const input = await new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
        data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
});

try {
    await mermaid.parse(input);
    process.exit(0);
} catch (err) {
    const message = err?.message ?? String(err);
    process.stderr.write(message);
    process.exit(1);
}
