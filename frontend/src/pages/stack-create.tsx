import { FormField } from "@/components/form-field";
import { type JsonSchema, SchemaForm } from "@/components/schema-form";
import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogActions,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { copyToClipboard } from "@/lib/utils";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Boxes,
  Plus,
  Trash2,
  ChevronRight,
  Container,
  Copy,
  FileCode2,
  Github,
  GitBranch,
  Globe2,
  Rocket,
  Search,
  Server,
  ToggleLeft,
  ToggleRight,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

// ─── Types ───────────────────────────────────────────────────────────────────
type Blueprint = components["schemas"]["BlueprintResponse"];
type BlueprintWithMeta = Blueprint & {
  category?: string;
  icon?: string;
  display_name?: string;
  description?: string;
};
type Accepted = components["schemas"]["StackOperationAccepted"];
type Step = "type" | "github_app" | "repository" | "public_git" | "docker_image" | "template";
type DomainConfig = { base_domain: string | null; sslip_fallback_ip: string | null };

const BUILD_PACKS = [
  { value: "nixpacks", label: "Nixpacks", available: true, hint: "Auto-detect build settings" },
  { value: "dockerfile", label: "Dockerfile", available: true, hint: "Build from a Dockerfile" },
  { value: "compose", label: "Docker Compose", available: true, hint: "Multi-container setup" },
];

// ─── Utilities ───────────────────────────────────────────────────────────────
export function blueprintDisplayName(bp: Blueprint): string {
  const meta = bp as BlueprintWithMeta;
  if (meta.display_name) return meta.display_name;
  const schemaTitle = (bp.inputs_schema as JsonSchema).title;
  if (schemaTitle && !schemaTitle.endsWith("Inputs")) return schemaTitle;
  return bp.id
    .split("-")
    .filter(Boolean)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
}

function inputDefaults(schema: JsonSchema): Record<string, unknown> {
  const d: Record<string, unknown> = {};
  for (const [k, f] of Object.entries(schema.properties ?? {})) {
    if (f.default !== undefined && f.default !== null && f.default !== "") d[k] = f.default;
  }
  return d;
}

function slugFromRepo(repo: string): string {
  const last = repo.trim().replace(/\/$/, "").split(/[/:]/).pop() ?? "";
  const cleaned = last
    .replace(/\.git$/i, "")
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned.slice(0, 32) || "my-app";
}

function validateSlug(v: string): string | null {
  return /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(v)
    ? null
    : "1–32 lowercase letters, digits or hyphens; no leading/trailing hyphen";
}

// Domain auto-generation (mirrors backend stacks_service.suggested_domain).
// Pattern: {name}.{apps_base_domain}  ─OR─  {name}.{public_ip}.sslip.io
function suggestedDomain(name: string, config: DomainConfig | null | undefined): string {
  const slug = name.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
  if (!slug) return "";
  const base = (config?.base_domain ?? "").trim().replace(/^\.+|\.+$/g, "");
  if (base) return `${slug}.${base}`;
  const ip = (config?.sslip_fallback_ip ?? "").trim();
  if (ip) return `${slug}.${ip}.sslip.io`;
  return "";
}

function domainHint(domain: string, name: string, config: DomainConfig | null | undefined): string | null {
  if (!domain) return null;
  if (domain !== suggestedDomain(name, config)) return null;
  const base = (config?.base_domain ?? "").trim();
  if (base) return `Auto-generated from your wildcard domain *.${base}`;
  if ((config?.sslip_fallback_ip ?? "").trim())
    return "Auto-generated via sslip.io — resolves to this server with zero DNS setup";
  return null;
}

// ─── ResourceCard (like Coolify's x-resource-view / coolbox) ─────────────────
function ResourceCard({
  icon: Icon,
  title,
  description,
  badge,
  disabled,
  onClick,
}: {
  icon: typeof GitBranch;
  title: string;
  description: string;
  badge?: string;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={[
        "group flex w-full cursor-pointer flex-col gap-3 rounded-lg border bg-card p-4 text-left transition-all",
        disabled
          ? "cursor-not-allowed border-border opacity-50"
          : "border-border hover:border-primary hover:bg-primary/5 hover:shadow-sm active:scale-[0.99]",
      ].join(" ")}
    >
      <div
        className={[
          "flex h-12 w-12 items-center justify-center rounded-lg border border-border bg-muted transition-colors",
          !disabled && "group-hover:border-primary/50 group-hover:bg-primary/10",
        ].join(" ")}
      >
        <Icon
          className={[
            "h-6 w-6 transition-colors",
            disabled ? "text-muted-foreground" : "text-muted-foreground group-hover:text-primary",
          ].join(" ")}
          aria-hidden
        />
      </div>
      <div>
        <div className="flex items-center gap-2">
          <span className="font-medium">{title}</span>
          {badge && (
            <Badge variant="outline" className="text-[10px] uppercase">
              {badge}
            </Badge>
          )}
        </div>
        <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>
      </div>
    </button>
  );
}

// ─── SourceAppCard (GitHub App selection) ────────────────────────────────────
function SourceAppCard({
  source,
  onClick,
}: {
  source: { id: number; name: string; app_slug?: string | null; installation_id?: string | null };
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={!source.installation_id}
      onClick={onClick}
      className={[
        "group flex w-full items-center gap-4 rounded-lg border bg-card py-4 px-5 text-left transition-all",
        !source.installation_id
          ? "cursor-not-allowed border-border opacity-60"
          : "cursor-pointer border-border hover:border-primary hover:bg-primary/5",
      ].join(" ")}
    >
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-muted">
        <Github className="h-5 w-5 text-muted-foreground" aria-hidden />
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-medium truncate">{source.name}</div>
        <div className="text-sm text-muted-foreground truncate">
          {source.app_slug ? `github.com/apps/${source.app_slug}` : "github.com"}
        </div>
        {!source.installation_id && (
          <div className="mt-1 text-xs text-destructive">Not installed — visit Sources to fix</div>
        )}
      </div>
      {source.installation_id && (
        <ChevronRight className="h-4 w-4 text-muted-foreground" aria-hidden />
      )}
    </button>
  );
}

// ─── BuildPackSelect ──────────────────────────────────────────────────────────
function BuildPackSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor="build-pack">Build Pack</Label>
      <select
        id="build-pack"
        value={value}
        onChange={(e) => {
          const bp = BUILD_PACKS.find((b) => b.value === e.target.value);
          if (bp && !bp.available) {
            toast.info(`${bp.label} is coming soon.`);
            return;
          }
          onChange(e.target.value);
        }}
        className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        {BUILD_PACKS.map((bp) => (
          <option key={bp.value} value={bp.value}>
            {bp.label}
            {!bp.available ? " (Coming Soon)" : ""}
          </option>
        ))}
      </select>
    </div>
  );
}

// ─── EnvVarsEditor ────────────────────────────────────────────────────────────

function EnvVarsEditor({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const [devView, setDevView] = useState(false);

  // Parse KEY=value string into array of objects
  const parseEnvVars = (str: string) => {
    if (!str.trim()) return [{ key: "", value: "" }];
    return str.split("\n").map((line) => {
      const idx = line.indexOf("=");
      if (idx === -1) return { key: line.trim(), value: "" };
      return { key: line.slice(0, idx).trim(), value: line.slice(idx + 1).trim() };
    });
  };

  const serializeEnvVars = (pairs: { key: string; value: string }[]) => {
    return pairs
      .filter((p) => p.key) // Only serialize rows with a key
      .map((p) => `${p.key}=${p.value}`)
      .join("\n");
  };

  const pairs = parseEnvVars(value);

  const updatePair = (index: number, newKey: string, newValue: string) => {
    const newPairs = [...pairs];
    newPairs[index] = { key: newKey, value: newValue };
    onChange(serializeEnvVars(newPairs));
  };

  const addPair = () => {
    const newPairs = [...pairs, { key: "", value: "" }];
    onChange(serializeEnvVars(newPairs));
  };

  const removePair = (index: number) => {
    const newPairs = pairs.filter((_, i) => i !== index);
    if (newPairs.length === 0) newPairs.push({ key: "", value: "" });
    onChange(serializeEnvVars(newPairs));
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label>Environment Variables</Label>
        <button
          type="button"
          onClick={() => setDevView(!devView)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          {devView ? (
            <>
              <ToggleRight className="h-3.5 w-3.5" aria-hidden /> Developer view
            </>
          ) : (
            <>
              <ToggleLeft className="h-3.5 w-3.5" aria-hidden /> Normal view
            </>
          )}
        </button>
      </div>
      
      <p className="text-xs text-muted-foreground">
        Stored encrypted and injected at runtime.
      </p>

      {devView ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={"PORT=3000\nDATABASE_URL=postgres://...\nSECRET_KEY=..."}
          rows={8}
          spellCheck={false}
          className="flex w-full rounded-md border border-input bg-background px-3 py-2.5 font-mono text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 resize-y mt-2"
        />
      ) : (
        <div className="space-y-2 mt-2">
          {pairs.map((p, i) => (
            <div key={i} className="flex gap-2 items-center">
              <Input 
                value={p.key} 
                onChange={(e) => updatePair(i, e.target.value, p.value)}
                placeholder="KEY"
                className="font-mono text-xs w-1/3"
              />
              <span className="text-muted-foreground">=</span>
              <Input 
                value={p.value} 
                onChange={(e) => updatePair(i, p.key, e.target.value)}
                placeholder="value"
                className="font-mono text-xs flex-1"
              />
              <Button type="button" variant="ghost" size="icon" onClick={() => removePair(i)}>
                <Trash2 className="h-4 w-4 text-destructive" />
              </Button>
            </div>
          ))}
          <Button type="button" variant="outline" size="sm" onClick={addPair} className="w-full mt-2">
            <Plus className="mr-2 h-4 w-4" /> Add Variable
          </Button>
        </div>
      )}
    </div>
  );
}

// ─── ConfigureForm (shared for git-based deployments) ────────────────────────
function ConfigureForm({
  repo,
  defaultBranch,
  sourceId,
  blueprint,
  domainConfig,
  onSubmit,
  pending,
  serverError,
}: {
  repo: string;
  defaultBranch?: string;
  sourceId: number | null;
  blueprint: Blueprint;
  domainConfig?: DomainConfig | null;
  onSubmit: (name: string, inputs: Record<string, unknown>) => Promise<void>;
  pending: boolean;
  serverError: string | null;
}) {
  const [branch, setBranch] = useState(defaultBranch || "main");
  const [buildPack, setBuildPack] = useState("nixpacks");
  const [composeContent, setComposeContent] = useState<string | null>(null);
  const [name, setName] = useState(() => slugFromRepo(repo));
  const [manualName, setManualName] = useState(false);
  const [port, setPort] = useState("3000");
  const [domain, setDomain] = useState(() => suggestedDomain(slugFromRepo(repo), domainConfig));
  const [manualDomain, setManualDomain] = useState(false);
  const [envVars, setEnvVars] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);

  useEffect(() => {
    if (!manualName && repo) setName(slugFromRepo(repo));
  }, [repo, manualName]);

  const analyze = useQuery({
    queryKey: ["git-analyze", repo, branch],
    enabled: !!repo && !!branch,
    queryFn: async () => {
      // @ts-ignore
      const { data, error } = await api.POST("/api/stacks/git/analyze", {
        body: { repo, branch },
      });
      if (error) return null;
      return data;
    },
  });

  useEffect(() => {
    if (analyze.data) {
      if (analyze.data.has_compose) {
        setBuildPack("compose");
        setComposeContent(analyze.data.compose_file_content || null);
      } else if (analyze.data.has_dockerfile) {
        setBuildPack("dockerfile");
      } else {
        setBuildPack("nixpacks");
      }
      
      const envKeys = analyze.data.env_keys;
      if (envKeys && envKeys.length > 0) {
        setEnvVars((prev) => {
          const currentKeys = new Set(prev.split("\n").map(l => (l.split("=")[0] || "").trim()).filter(Boolean));
          let newEnv = prev;
          for (const k of envKeys) {
            if (!currentKeys.has(k)) {
              newEnv += (newEnv ? "\n" : "") + `${k}=`;
            }
          }
          return newEnv;
        });
      }
    }
  }, [analyze.data]);

  useEffect(() => {
    if (defaultBranch) setBranch(defaultBranch);
  }, [defaultBranch]);

  // When name changes, keep domain in sync unless user has manually set it.
  useEffect(() => {
    if (!manualDomain) setDomain(suggestedDomain(name, domainConfig));
  }, [name, domainConfig, manualDomain]);

  const handleSubmit = async () => {
    const slug = name.trim().toLowerCase();
    const slugError = validateSlug(slug);
    if (slugError) {
      setNameError(slugError);
      return;
    }
    setNameError(null);

    const parsedEnv: Record<string, string> = {};
    for (const line of envVars.split("\n")) {
      const m = line.match(/^([^=]+)=(.*)/);
      if (m && m[1]?.trim()) parsedEnv[m[1].trim()] = m[2]?.trim() ?? "";
    }

    await onSubmit(slug, {
      ...inputDefaults((blueprint.inputs_schema || {}) as JsonSchema),
      repo: repo.trim(),
      branch: branch.trim() || "main",
      build_method: buildPack,
      internal_port: Number(port) || 3000,
      domain: domain.trim(),
      env: parsedEnv,
      source_id: sourceId,
      compose_file_content: composeContent || "",
    });
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-bold">Configuration</h2>
        <p className="text-sm text-muted-foreground">
          Configure your deployment. You can change these later.
        </p>
      </div>

      {/* Repository display (read-only, matches Coolify's configure step) */}
      <div className="space-y-1.5">
        <Label>Repository</Label>
        <Input value={repo} disabled className="font-mono text-xs" />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <FormField label="Branch" htmlFor="cfg-branch">
          <Input
            id="cfg-branch"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            placeholder="main"
            spellCheck={false}
          />
        </FormField>
        <BuildPackSelect value={buildPack} onChange={setBuildPack} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <FormField label="Port" htmlFor="cfg-port">
          <Input
            id="cfg-port"
            type="number"
            min={1}
            max={65535}
            value={port}
            onChange={(e) => setPort(e.target.value)}
          />
        </FormField>
        <div className="space-y-1.5">
          <Label htmlFor="cfg-domain">Domain (optional)</Label>
          <Input
            id="cfg-domain"
            placeholder="app.example.com"
            value={domain}
            onChange={(e) => {
              setManualDomain(true);
              setDomain(e.target.value);
            }}
            spellCheck={false}
          />
          {domainHint(domain, name, domainConfig) && (
            <p className="text-xs text-muted-foreground">
              {domainHint(domain, name, domainConfig)}
            </p>
          )}
        </div>
      </div>

      <FormField label="Deployment Name" htmlFor="cfg-name" error={nameError ?? undefined}>
        <Input
          id="cfg-name"
          value={name}
          onChange={(e) => {
            setManualName(true);
            setName(e.target.value);
          }}
          spellCheck={false}
        />
      </FormField>

      <EnvVarsEditor value={envVars} onChange={setEnvVars} />

      {serverError && (
        <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
          {serverError}
        </p>
      )}

      <Button
        onClick={handleSubmit}
        loading={pending}
        disabled={!repo.trim()}
        className="w-full sm:w-auto"
      >
        <Rocket className="mr-2 h-4 w-4" aria-hidden />
        Continue
      </Button>
    </div>
  );
}

// ─── ShowOnceDialog ───────────────────────────────────────────────────────────
export function ShowOnceDialog({
  secrets,
  onClose,
}: {
  secrets: Record<string, string> | null;
  onClose: () => void;
}) {
  const copy = async (v: string) => {
    try {
      await copyToClipboard(v);
      toast.success("Copied");
    } catch {
      toast.error("Copy failed — select the text manually.");
    }
  };

  return (
    <Dialog open={secrets !== null} onClose={onClose}>
      <DialogContent>
        <DialogTitle>Generated secrets</DialogTitle>
        <DialogDescription>These values are shown once and stored encrypted.</DialogDescription>
        {secrets && (
          <dl className="space-y-2 text-sm">
            {Object.entries(secrets).map(([label, value]) => (
              <div key={label} className="flex items-center justify-between gap-2">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="flex items-center gap-1 font-mono text-xs">
                  <span className="break-all">{value}</span>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Copy ${label}`}
                    onClick={() => copy(value)}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                </dd>
              </div>
            ))}
          </dl>
        )}
        <DialogActions>
          <Button onClick={onClose}>Continue</Button>
        </DialogActions>
      </DialogContent>
    </Dialog>
  );
}

// ─── TemplateDeployForm ───────────────────────────────────────────────────────
function TemplateDeployForm({
  blueprints,
  onSubmit,
  pending,
  serverError,
}: {
  blueprints: Blueprint[];
  onSubmit: (blueprint: Blueprint, name: string, inputs: Record<string, unknown>) => Promise<void>;
  pending: boolean;
  serverError: string | null;
}) {
  const templates = blueprints.filter((bp) => !["git", "raw-image"].includes(bp.id));
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Blueprint | null>(templates[0] ?? null);
  const [name, setName] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);

  useEffect(() => {
    if (selected) {
      setName(`${selected.id}-${Math.random().toString(36).slice(2, 6)}`);
      setNameError(null);
    }
  }, [selected]);

  const filtered = templates.filter((bp) => {
    const term = query.trim().toLowerCase();
    return !term || bp.id.includes(term) || blueprintDisplayName(bp).toLowerCase().includes(term);
  });

  if (!selected) return <ErrorState message="No one-click templates are available." />;

  return (
    <div className="grid gap-5 lg:grid-cols-[280px_1fr]">
      <div className="space-y-3">
        <h2 className="font-bold text-lg">Templates</h2>
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden
          />
          <Input
            aria-label="Search templates"
            placeholder="Search templates"
            className="pl-9"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="max-h-[520px] space-y-1.5 overflow-y-auto pr-1">
          {filtered.map((bp) => {
            const active = selected.id === bp.id;
            const meta = bp as BlueprintWithMeta;
            return (
              <button
                type="button"
                key={bp.id}
                onClick={() => setSelected(bp)}
                className={[
                  "flex w-full cursor-pointer items-center gap-3 rounded-lg border p-3 text-left transition-colors",
                  active
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border bg-card hover:border-primary/60 hover:bg-accent/50",
                ].join(" ")}
              >
                <Boxes
                  className={[
                    "h-5 w-5 shrink-0",
                    active ? "text-primary-foreground" : "text-muted-foreground",
                  ].join(" ")}
                  aria-hidden
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">
                    {blueprintDisplayName(bp)}
                  </span>
                  <span
                    className={[
                      "block text-xs",
                      active ? "opacity-80" : "text-muted-foreground",
                    ].join(" ")}
                  >
                    {meta.category || "Template"}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="rounded-lg border border-border bg-card p-5">
        <SchemaForm
          key={selected.id}
          schema={selected.inputs_schema as JsonSchema}
          onSubmit={async (inputs) => {
            const slug = name.trim().toLowerCase();
            const slugError = validateSlug(slug);
            if (slugError) {
              setNameError(slugError);
              return;
            }
            setNameError(null);
            await onSubmit(selected, slug, inputs);
          }}
          submitLabel={`Deploy ${blueprintDisplayName(selected)}`}
          pending={pending}
          serverError={serverError}
        >
          <FormField label="Deployment name" htmlFor="template-name" error={nameError ?? undefined}>
            <Input
              id="template-name"
              autoComplete="off"
              spellCheck={false}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </FormField>
        </SchemaForm>
      </div>
    </div>
  );
}

// ─── StackCreatePage ──────────────────────────────────────────────────────────
export function StackCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // wizard state
  const [step, setStep] = useState<Step>("type");
  const [selectedSourceId, setSelectedSourceId] = useState<number | null>(null);
  const [selectedRepo, setSelectedRepo] = useState<{
    name: string;
    clone_url: string;
    default_branch: string;
  } | null>(null);
  const [publicRepoUrl, setPublicRepoUrl] = useState("");
  const [publicChecked, setPublicChecked] = useState(false);

  // server selection (multi-server deployment)
  const [serverId, setServerId] = useState<number | null>(null);

  // submit state
  const [serverError, setServerError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [accepted, setAccepted] = useState<Accepted | null>(null);

  // data queries
  const blueprints = useQuery({
    queryKey: ["stack-blueprints"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/stacks/blueprints");
      if (error || !data)
        throw new Error(apiErrorMessage(error, "Failed to load deployment options"));
      return data; /* as any */
    },
  });

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sources", {});
      if (error || !data) return [];
      return data as any[];
    },
  });

  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: async () => {
      const { data } = await (api as any).GET("/api/servers");
      return (data ?? []) as { id: number; name: string; is_localhost: boolean; status: string }[];
    },
    staleTime: 30_000,
  });

  const domainConfig = useQuery({
    queryKey: ["apps-base-domain"],
    staleTime: 60_000,
    queryFn: async () => {
      const { data } = await (api as any).GET("/api/system/apps-base-domain");
      return (data ?? null) as DomainConfig | null;
    },
  });

  const repos = useQuery({
    queryKey: ["sources", selectedSourceId, "repos"],
    enabled: step === "repository" && selectedSourceId !== null,
    retry: 1,
    queryFn: async () => {
      const { data, error } = await (api as any).GET("/api/sources/{source_id}/repos", {
        params: { path: { source_id: selectedSourceId } },
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load repositories"));
      return data as { name: string; clone_url: string; default_branch: string }[];
    },
  });

  const byId = useMemo(() => {
    const map = new Map<string, Blueprint>();
    for (const bp of blueprints.data ?? []) map.set(bp.id, bp);
    return map;
  }, [blueprints.data]);

  const gitBlueprint = byId.get("git");
  const imageBlueprint = byId.get("raw-image");

  // ── submit ──
  const submitBlueprint = async (
    blueprint: Blueprint,
    name: string,
    inputs: Record<string, unknown>,
  ) => {
    setServerError(null);
    setPending(true);
    try {
      const { data, error, response } = await api.POST("/api/stacks", {
        body: { name, blueprint_id: blueprint.id, inputs, server_id: serverId ?? null },
      });
      if (error || !data) {
        setServerError(apiErrorMessage(error, `Deploy failed (${response.status})`));
        return;
      }
      await queryClient.invalidateQueries({ queryKey: ["stacks"] });
      toast.success(`Deploying ${data.stack.name}`);
      if (Object.keys(data.show_once ?? {}).length > 0) {
        setAccepted(data);
      } else {
        navigate(`/stacks/${data.stack.id}`, { state: { operationId: data.operation_id } });
      }
    } finally {
      setPending(false);
    }
  };

  // ── breadcrumb back navigation ──
  const stepLabels: Partial<Record<Step, string>> = {
    github_app: "Select GitHub App",
    repository: "Select Repository",
    public_git: "Public Repository",
    docker_image: "Docker Image",
    template: "Templates",
  };

  const goBack = () => {
    if (step === "repository") {
      setSelectedRepo(null);
      setStep("github_app");
    } else {
      setStep("type");
      setSelectedSourceId(null);
      setSelectedRepo(null);
      setPublicChecked(false);
    }
    setServerError(null);
  };

  // ─── RENDER ───────────────────────────────────────────────────────────────
  return (
    <div className="mx-auto max-w-5xl space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Button
          variant="ghost"
          size="icon"
          aria-label="Back"
          onClick={() => (step === "type" ? navigate("/stacks") : goBack())}
        >
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Create a new Application</h1>
          <p className="text-sm text-muted-foreground">
            Deploy resources like applications, databases, services.
          </p>
        </div>
      </div>

      {/* Breadcrumb trail (when not on type step) */}
      {step !== "type" && (
        <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
          <button
            type="button"
            onClick={() => { setStep("type"); setSelectedSourceId(null); setSelectedRepo(null); }}
            className="hover:text-foreground hover:underline"
          >
            New Resource
          </button>
          <ChevronRight className="h-3.5 w-3.5" aria-hidden />
          <span className="text-foreground">{stepLabels[step]}</span>
          {step === "repository" && selectedRepo && (
            <>
              <ChevronRight className="h-3.5 w-3.5" aria-hidden />
              <span className="text-foreground">{selectedRepo.name}</span>
            </>
          )}
        </div>
      )}

      {/* Server picker — shown when multiple servers are available */}
      {(servers.data ?? []).length > 1 && (
        <div className="flex items-center gap-3 rounded-lg border bg-muted/30 px-4 py-2.5">
          <Server className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <span className="text-sm font-medium">Deploy to:</span>
          <select
            value={serverId ?? ""}
            onChange={(e) => setServerId(e.target.value ? Number(e.target.value) : null)}
            className="ml-auto h-8 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="">Localhost (default)</option>
            {(servers.data ?? [])
              .filter((s) => !s.is_localhost)
              .map((s) => (
                <option key={s.id} value={s.id} disabled={s.status !== "connected"}>
                  {s.name}{s.status !== "connected" ? " (not connected)" : ""}
                </option>
              ))}
          </select>
        </div>
      )}

      {/* Loading state */}
      {blueprints.isPending && <LoadingState label="Loading deployment options" />}
      {blueprints.isError && (
        <ErrorState message={blueprints.error.message} onRetry={() => blueprints.refetch()} />
      )}

      {blueprints.data && (
        <>
          {/* ── STEP: type selection ─────────────────────────────────────── */}
          {step === "type" && (
            <div className="space-y-8">
              {/* Applications */}
              <div>
                <h2 className="mb-1 text-base font-semibold">Applications</h2>
                <p className="mb-4 text-sm text-muted-foreground">
                  Deploy resources, like Applications, Databases, Services…
                </p>
                <div className="grid gap-8 lg:grid-cols-2">
                  {/* Git Based column */}
                  <div className="space-y-4">
                    <h4 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                      Git Based
                    </h4>
                    <div className="grid gap-3">
                      <ResourceCard
                        icon={Globe2}
                        title="Public Repository"
                        description="Deploy any public repository from GitHub, GitLab, Bitbucket, or any Git URL."
                        onClick={() => { setStep("public_git"); setServerError(null); }}
                      />
                      <ResourceCard
                        icon={Github}
                        title="Private Repository (GitHub App)"
                        description="Deploy private repositories using a connected GitHub App."
                        onClick={() => { setStep("github_app"); setServerError(null); }}
                      />
                    </div>
                  </div>

                  {/* Docker Based column */}
                  <div className="space-y-4">
                    <h4 className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                      Docker Based
                    </h4>
                    <div className="grid gap-3">
                      <ResourceCard
                        icon={Container}
                        title="Docker Image"
                        description="Deploy a pre-built image from Docker Hub, GHCR, or a private registry."
                        onClick={() => { setStep("docker_image"); setServerError(null); }}
                      />
                      <ResourceCard
                        icon={FileCode2}
                        title="Dockerfile"
                        description="Write or paste a Dockerfile directly to build and deploy."
                        badge="Soon"
                        disabled
                        onClick={() => {}}
                      />
                    </div>
                  </div>
                </div>
              </div>

              {/* Templates */}
              {blueprints.data.some((bp) => !["git", "raw-image"].includes(bp.id)) && (
                <div>
                  <h2 className="mb-1 text-base font-semibold">Services</h2>
                  <p className="mb-4 text-sm text-muted-foreground">
                    One-click service templates — databases, tools, and more.
                  </p>
                  <ResourceCard
                    icon={Boxes}
                    title="One-Click Templates"
                    description="PostgreSQL, Redis, WordPress, and other pre-configured services."
                    onClick={() => { setStep("template"); setServerError(null); }}
                  />
                </div>
              )}
            </div>
          )}

          {/* ── STEP: GitHub App selection ───────────────────────────────── */}
          {step === "github_app" && (
            <div className="space-y-4">
              <h2 className="font-bold text-lg">Select a GitHub App</h2>
              <p className="text-sm text-muted-foreground">
                Deploy any public or private Git repositories through a GitHub App.
              </p>

              {sources.isPending && <LoadingState />}
              {sources.isError && (
                <ErrorState message="Failed to load GitHub Apps" onRetry={() => sources.refetch()} />
              )}
              {sources.data && sources.data.length === 0 && (
                <div className="rounded-lg border border-dashed border-border p-8 text-center">
                  <Github className="mx-auto mb-3 h-10 w-10 text-muted-foreground/50" aria-hidden />
                  <p className="font-medium">No GitHub App found.</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Please create a new GitHub App first.
                  </p>
                  <Button
                    className="mt-4"
                    variant="outline"
                    onClick={() => navigate("/sources")}
                  >
                    Go to Sources
                  </Button>
                </div>
              )}
              {sources.data && sources.data.length > 0 && (
                <div className="flex flex-col gap-2">
                  {sources.data.map((source: any) => (
                    <SourceAppCard
                      key={source.id}
                      source={source}
                      onClick={() => {
                        setSelectedSourceId(source.id);
                        setStep("repository");
                        setSelectedRepo(null);
                      }}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ── STEP: Repository selection + configure ────────────────────── */}
          {step === "repository" && (
            <div className="space-y-6">
              <h2 className="font-bold text-lg">Select Repository</h2>

              {repos.isPending && <LoadingState label="Loading repositories…" />}
              {repos.isError && (
                <div className="space-y-2">
                  <ErrorState
                    message={
                      repos.error instanceof Error ? repos.error.message : "Failed to load repositories"
                    }
                    onRetry={() => repos.refetch()}
                  />
                </div>
              )}

              {repos.data && repos.data.length === 0 && (
                <div className="rounded-lg border border-dashed border-border p-8 text-center">
                  <p className="font-medium">No repositories found.</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Check your GitHub App configuration.
                  </p>
                </div>
              )}

              {repos.data && repos.data.length > 0 && (
                <div className="space-y-6">
                  {/* Repository list as clickable cards (matches Coolify Select Repository step) */}
                  {!selectedRepo && (
                    <div>
                      <Label className="mb-3 block">Select a repository</Label>
                      <div className="max-h-[420px] space-y-1.5 overflow-y-auto pr-1">
                        {repos.data.map((r) => (
                          <button
                            key={r.clone_url}
                            type="button"
                            onClick={() => setSelectedRepo(r)}
                            className="group flex w-full items-center gap-4 rounded-lg border border-border bg-card px-4 py-3 text-left transition-all hover:border-primary hover:bg-primary/5"
                          >
                            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-muted group-hover:border-primary/40 group-hover:bg-primary/10 transition-colors">
                              <GitBranch className="h-4 w-4 text-muted-foreground group-hover:text-primary transition-colors" aria-hidden />
                            </div>
                            <div className="flex-1 min-w-0">
                              <p className="font-medium text-sm truncate">{r.name}</p>
                              <p className="text-xs text-muted-foreground truncate font-mono">
                                {r.clone_url.replace(/^https?:\/\//, "")}
                              </p>
                            </div>
                            <div className="flex items-center gap-2 shrink-0">
                              <Badge variant="outline" className="text-xs font-mono">
                                {r.default_branch}
                              </Badge>
                              <ChevronRight className="h-4 w-4 text-muted-foreground" aria-hidden />
                            </div>
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {selectedRepo && gitBlueprint && (
                    <ConfigureForm
                      repo={selectedRepo.clone_url}
                      defaultBranch={selectedRepo.default_branch}
                      sourceId={selectedSourceId}
                      blueprint={gitBlueprint}
                      domainConfig={domainConfig.data}
                      pending={pending}
                      serverError={serverError}
                      onSubmit={(name, inputs) => submitBlueprint(gitBlueprint, name, inputs)}
                    />
                  )}
                </div>
              )}
            </div>
          )}

          {/* ── STEP: Public Git ──────────────────────────────────────────── */}
          {step === "public_git" && (
            <div className="space-y-6">
              <div>
                <h2 className="font-bold text-lg">Create a new Application</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Deploy any public Git repository.
                </p>
              </div>

              <div className="space-y-3">
                <FormField label="Repository URL (https://)" htmlFor="public-repo-url">
                  <div className="flex gap-2">
                    <Input
                      id="public-repo-url"
                      placeholder="https://github.com/acme/app"
                      value={publicRepoUrl}
                      onChange={(e) => {
                        setPublicRepoUrl(e.target.value);
                        setPublicChecked(false);
                      }}
                      spellCheck={false}
                      autoFocus
                    />
                    <Button
                      type="button"
                      variant="outline"
                      disabled={!publicRepoUrl.trim()}
                      onClick={() => setPublicChecked(true)}
                    >
                      Check repository
                    </Button>
                  </div>
                </FormField>

                {!publicChecked && (
                  <p className="text-xs text-muted-foreground">
                    For example application deployments, check out{" "}
                    <a
                      href="https://github.com/coollabsio/coolify-examples"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline"
                    >
                      Coolify Examples
                    </a>
                    .
                  </p>
                )}
              </div>

              {publicChecked && gitBlueprint && (
                <ConfigureForm
                  repo={publicRepoUrl}
                  defaultBranch="main"
                  sourceId={null}
                  blueprint={gitBlueprint}
                  domainConfig={domainConfig.data}
                  pending={pending}
                  serverError={serverError}
                  onSubmit={(name, inputs) => submitBlueprint(gitBlueprint, name, inputs)}
                />
              )}
            </div>
          )}

          {/* ── STEP: Docker Image ────────────────────────────────────────── */}
          {step === "docker_image" && (
            <DockerImageView
              blueprint={imageBlueprint}
              domainConfig={domainConfig.data}
              pending={pending}
              serverError={serverError}
              onSubmit={(name, inputs) => {
                if (!imageBlueprint) return Promise.resolve();
                return submitBlueprint(imageBlueprint, name, inputs);
              }}
            />
          )}

          {/* ── STEP: Templates ──────────────────────────────────────────── */}
          {step === "template" && (
            <TemplateDeployForm
              blueprints={blueprints.data}
              pending={pending}
              serverError={serverError}
              onSubmit={submitBlueprint}
            />
          )}
        </>
      )}

      <ShowOnceDialog
        secrets={accepted?.show_once ?? null}
        onClose={() => {
          if (accepted) {
            navigate(`/stacks/${accepted.stack.id}`, {
              state: { operationId: accepted.operation_id },
            });
          }
        }}
      />
    </div>
  );
}

// ─── Docker Image view ────────────────────────────────────────────────────────
function DockerImageView({
  blueprint,
  domainConfig,
  onSubmit,
  pending,
  serverError,
}: {
  blueprint?: Blueprint;
  domainConfig?: DomainConfig | null;
  onSubmit: (name: string, inputs: Record<string, unknown>) => Promise<void>;
  pending: boolean;
  serverError: string | null;
}) {
  const [image, setImage] = useState("");
  const [tag, setTag] = useState("latest");
  const [name, setName] = useState("container-app");
  const [port, setPort] = useState("3000");
  const [domain, setDomain] = useState(() => suggestedDomain("container-app", domainConfig));
  const [manualDomain, setManualDomain] = useState(false);
  const [envVars, setEnvVars] = useState("");

  useEffect(() => {
    if (!manualDomain) setDomain(suggestedDomain(name, domainConfig));
  }, [name, domainConfig, manualDomain]);
  const [nameError, setNameError] = useState<string | null>(null);

  if (!blueprint) {
    return <ErrorState message="Container image deployment is not available in this build." />;
  }

  const handleSubmit = async () => {
    const slug = name.trim().toLowerCase();
    const slugError = validateSlug(slug);
    if (slugError) {
      setNameError(slugError);
      return;
    }
    setNameError(null);

    const parsedEnv: Record<string, string> = {};
    for (const line of envVars.split("\n")) {
      const m = line.match(/^([^=]+)=(.*)/);
      if (m && m[1]?.trim()) parsedEnv[m[1].trim()] = m[2]?.trim() ?? "";
    }

    await onSubmit(slug, {
      ...inputDefaults((blueprint.inputs_schema || {}) as JsonSchema),
      image: tag ? `${image.trim()}:${tag.trim()}` : image.trim(),
      internal_port: Number(port) || 3000,
      domain: domain.trim(),
      env: parsedEnv,
    });
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="font-bold text-lg">Docker Image</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Deploy a pre-built image from Docker Hub, GHCR, or a private registry.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <FormField label="Image Name" htmlFor="img-name-ref">
          <Input
            id="img-name-ref"
            placeholder="ghcr.io/acme/app"
            value={image}
            onChange={(e) => setImage(e.target.value)}
            spellCheck={false}
            autoFocus
          />
        </FormField>
        <FormField label="Tag (optional)" htmlFor="img-tag">
          <Input
            id="img-tag"
            placeholder="latest"
            value={tag}
            onChange={(e) => setTag(e.target.value)}
            spellCheck={false}
          />
        </FormField>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <FormField label="Deployment Name" htmlFor="img-dep-name" error={nameError ?? undefined}>
          <Input
            id="img-dep-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            spellCheck={false}
          />
        </FormField>
        <FormField label="Port" htmlFor="img-port">
          <Input
            id="img-port"
            type="number"
            min={1}
            max={65535}
            value={port}
            onChange={(e) => setPort(e.target.value)}
          />
        </FormField>
        <div className="space-y-1.5">
          <Label htmlFor="img-domain">Domain (optional)</Label>
          <Input
            id="img-domain"
            placeholder="app.example.com"
            value={domain}
            onChange={(e) => {
              setManualDomain(true);
              setDomain(e.target.value);
            }}
            spellCheck={false}
          />
          {domainHint(domain, name, domainConfig) && (
            <p className="text-xs text-muted-foreground">
              {domainHint(domain, name, domainConfig)}
            </p>
          )}
        </div>
      </div>

      <EnvVarsEditor value={envVars} onChange={setEnvVars} />

      {serverError && (
        <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
          {serverError}
        </p>
      )}

      <Button
        onClick={handleSubmit}
        loading={pending}
        disabled={!image.trim()}
        className="w-full sm:w-auto"
      >
        <Rocket className="mr-2 h-4 w-4" aria-hidden />
        Continue
      </Button>
    </div>
  );
}
