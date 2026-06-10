#!/usr/bin/env node

import { spawn } from "node:child_process";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { login, start } from "/Users/admin/Documents/朱总的秘书/weixin-agent-sdk/packages/sdk/dist/index.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..");
const pythonBin = process.env.QUANT_WECHAT_PYTHON || "python3";

function runQuantChat(message) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      pythonBin,
      ["-m", "quant_wechat_bot.bot_service", "chat", message],
      {
        cwd: repoRoot,
        env: process.env,
        stdio: ["ignore", "pipe", "pipe"],
      },
    );

    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error("quant bot timed out after 30s"));
    }, 30_000);

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });

    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });

    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code === 0) {
        resolve(stdout.trim() || "机器人暂时没有返回内容。");
        return;
      }
      reject(new Error((stderr || stdout || `python exited with code ${code}`).trim()));
    });
  });
}

const agent = {
  async chat(request) {
    const text = String(request.text || "").trim();
    if (!text) {
      return {
        text: "先发文字给我就行，比如：策略列表、选股 质量、评分 NVDA。",
      };
    }

    if (request.media) {
      return {
        text: "当前这个量化机器人先专注文本消息。你可以直接发：选股 质量、选股 动量、评分 NVDA、股票池。",
      };
    }

    try {
      const reply = await runQuantChat(text);
      return { text: reply };
    } catch (error) {
      return {
        text: `量化机器人执行失败：${String(error)}\n\n你可以先试：帮助`,
      };
    }
  },
};

async function main() {
  const command = process.argv[2] || "start";

  if (command === "login") {
    await login();
    return;
  }

  if (command === "chat") {
    const message = process.argv.slice(3).join(" ").trim();
    if (!message) {
      console.error("用法: node weixin_personal_agent.mjs chat <message>");
      process.exit(1);
    }
    console.log(await runQuantChat(message));
    return;
  }

  if (command !== "start") {
    console.log(`Quant WeChat personal agent

用法:
  node quant_wechat_bot/weixin_personal_agent.mjs login
  node quant_wechat_bot/weixin_personal_agent.mjs start
  node quant_wechat_bot/weixin_personal_agent.mjs chat "选股 质量"
`);
    return;
  }

  const ac = new AbortController();
  process.on("SIGINT", () => {
    console.log("\n正在停止...");
    ac.abort();
  });
  process.on("SIGTERM", () => ac.abort());

  const bot = start(agent, { abortSignal: ac.signal });
  await bot.wait();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
