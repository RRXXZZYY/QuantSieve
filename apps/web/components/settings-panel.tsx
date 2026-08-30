"use client";

import { useState } from "react";

import { SettingsIcon } from "./icons";

export type LlmSettings = {
  apiKey: string;
  baseUrl: string;
  model: string;
};

const DEFAULTS: LlmSettings = {
  apiKey: "",
  baseUrl: "https://api.openai.com/v1",
  model: "gpt-4.1-mini",
};

export function readLlmSettings(): LlmSettings {
  if (typeof window === "undefined") return DEFAULTS;
  const stored = window.localStorage.getItem("quantsieve-llm");
  return stored ? ({ ...DEFAULTS, ...JSON.parse(stored) } as LlmSettings) : DEFAULTS;
}

export function SettingsPanel() {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<LlmSettings>(DEFAULTS);

  function show() {
    setSettings(readLlmSettings());
    setOpen(true);
  }

  function save() {
    window.localStorage.setItem("quantsieve-llm", JSON.stringify(settings));
    setOpen(false);
  }

  return (
    <>
      <button className="icon-button" onClick={show} title="模型设置">
        <SettingsIcon />
      </button>
      {open && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setOpen(false)}>
          <section
            aria-label="模型设置"
            className="settings-card"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="section-heading">
              <div>
                <span className="eyebrow">BYOK</span>
                <h2>连接你的模型</h2>
              </div>
              <button className="text-button" onClick={() => setOpen(false)}>
                关闭
              </button>
            </div>
            <p className="muted">
              Key 只保存在当前浏览器，并随本次请求直达你选择的兼容接口。
            </p>
            <label>
              API Key
              <input
                onChange={(event) => setSettings({ ...settings, apiKey: event.target.value })}
                placeholder="sk-..."
                type="password"
                value={settings.apiKey}
              />
            </label>
            <label>
              Base URL
              <input
                onChange={(event) => setSettings({ ...settings, baseUrl: event.target.value })}
                value={settings.baseUrl}
              />
            </label>
            <label>
              Model
              <input
                onChange={(event) => setSettings({ ...settings, model: event.target.value })}
                value={settings.model}
              />
            </label>
            <button className="primary-button full" onClick={save}>
              保存设置
            </button>
          </section>
        </div>
      )}
    </>
  );
}
