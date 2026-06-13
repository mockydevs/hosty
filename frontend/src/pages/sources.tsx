import { ErrorState, LoadingState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api/client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Github, Plus, RefreshCw, Trash2, Zap } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";

const ADJECTIVES = [
  "admiring", "agile", "amazing", "bold", "brave", "bright", "calm", "clever",
  "dazzling", "eager", "epic", "fervent", "focused", "friendly", "gentle", "graceful",
  "happy", "keen", "kind", "lively", "lucky", "magical", "nimble", "peaceful",
  "proud", "quick", "rapid", "resilient", "serene", "sharp", "sleek", "smart",
  "swift", "tender", "vibrant", "vigilant", "wise", "witty",
];
const NOUNS = [
  "albatross", "badger", "beaver", "bison", "cardinal", "crane", "crow", "eagle",
  "falcon", "finch", "fox", "gecko", "grouse", "hawk", "heron", "jaguar",
  "kestrel", "kite", "lark", "lynx", "merlin", "moose", "osprey", "otter",
  "panther", "puffin", "raven", "robin", "seal", "sparrow", "swift", "tiger",
  "viper", "wagtail", "weasel", "wolf", "wren",
];

function generateAppName(): string {
  const adj = ADJECTIVES[Math.floor(Math.random() * ADJECTIVES.length)];
  const noun = NOUNS[Math.floor(Math.random() * NOUNS.length)];
  const suffix = Math.random().toString(36).slice(2, 10);
  return `${adj}-${noun}-${suffix}`;
}

export function GitHubInstallPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const called = useRef(false);

  useEffect(() => {
    if (called.current) return;
    called.current = true;

    const installation_id = searchParams.get("installation_id");
    const setup_action = searchParams.get("setup_action");
    const state = searchParams.get("state");

    if (!installation_id) {
      navigate("/sources", { replace: true });
      return;
    }

    api
      .POST("/api/sources/github/install" as any, {
        body: { installation_id, setup_action, state } as any,
      })
      .then(({ error: apiError }) => {
        if (apiError) {
          setError(apiErrorMessage(apiError, "Failed to save installation"));
          return;
        }
        navigate("/sources", { replace: true });
      });
  }, [searchParams, navigate]);

  if (error) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center p-4">
        <div className="max-w-md w-full">
          <ErrorState message={error} />
          <div className="mt-4 flex justify-center">
            <Button variant="outline" onClick={() => navigate("/sources")}>
              Back to Sources
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return <LoadingState label="Saving GitHub App installation…" />;
}

export function GitHubCallbackPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const called = useRef(false);

  useEffect(() => {
    if (called.current) return;
    called.current = true;

    const code = searchParams.get("code");
    const installation_id = searchParams.get("installation_id");

    if (!code) {
      setError("Missing code parameter from GitHub — the app registration may have been cancelled.");
      return;
    }

    api
      .POST("/api/sources/github/callback" as any, {
        body: { code, installation_id } as any,
      })
      .then(({ data, error: apiError }) => {
        if (apiError || !data) {
          setError(apiErrorMessage(apiError, "Failed to complete GitHub App setup"));
          return;
        }
        navigate("/sources", { replace: true });
      });
  }, [searchParams, navigate]);

  if (error) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center p-4">
        <div className="max-w-md w-full">
          <ErrorState message={error} />
          <div className="mt-4 flex justify-center">
            <Button variant="outline" onClick={() => navigate("/sources")}>
              Back to Sources
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return <LoadingState label="Completing GitHub App registration…" />;
}

export function SourcesPage() {
  const [addOpen, setAddOpen] = useState(false);
  const queryClient = useQueryClient();

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/sources", {});
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to load sources"));
      return data as any[];
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (id: number) => {
      const { error } = await (api as any).DELETE("/api/sources/{source_id}", {
        params: { path: { source_id: id } },
      });
      if (error) throw new Error(apiErrorMessage(error, "Failed to delete source"));
    },
    onSuccess: () => {
      toast.success("Source removed");
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Delete failed"),
  });

  const refreshMutation = useMutation({
    mutationFn: async (id: number) => {
      const { data, error } = await (api as any).POST(
        "/api/sources/{source_id}/refresh-installation",
        { params: { path: { source_id: id } } },
      );
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to refresh installation"));
      return data as { installation_id: string };
    },
    onSuccess: (data) => {
      toast.success(`Installation ID updated: ${data.installation_id}`);
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Refresh failed"),
  });

  const installMutation = useMutation({
    mutationFn: async (id: number) => {
      const { data, error } = await (api as any).GET(
        "/api/sources/{source_id}/install-url",
        { params: { path: { source_id: id } } },
      );
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to get install URL"));
      return data as { url: string };
    },
    onSuccess: (data) => {
      window.location.href = data.url;
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Failed"),
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Sources</h1>
          <p className="text-sm text-muted-foreground">Git sources for your applications.</p>
        </div>
        <Button onClick={() => setAddOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden /> Add
        </Button>
      </div>

      {sources.isPending ? (
        <LoadingState />
      ) : sources.isError ? (
        <ErrorState message={sources.error.message} onRetry={() => sources.refetch()} />
      ) : sources.data.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-12 text-center">
            <Github className="mb-4 h-12 w-12 text-muted-foreground/50" aria-hidden />
            <h2 className="text-lg font-semibold">No sources found</h2>
            <p className="mb-4 text-sm text-muted-foreground max-w-sm">
              Connect a GitHub account to easily deploy your repositories.
            </p>
            <Button onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" aria-hidden /> Connect GitHub
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {sources.data.map((source) => (
            <Card key={source.id}>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center justify-between text-base">
                  <div className="flex items-center gap-2">
                    <Github className="h-4 w-4" aria-hidden />
                    {source.name}
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant="outline" className="text-xs">
                      {source.provider}
                    </Badge>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 text-muted-foreground hover:text-destructive"
                      aria-label={`Remove ${source.name}`}
                      loading={deleteMutation.isPending}
                      onClick={() => deleteMutation.mutate(source.id)}
                    >
                      <Trash2 className="h-3.5 w-3.5" aria-hidden />
                    </Button>
                  </div>
                </CardTitle>
                <CardDescription className="font-mono text-xs text-muted-foreground space-y-1">
                  <span className="block">App ID: {source.app_id}</span>
                  {source.installation_id ? (
                    <span className="block text-green-500">
                      Install ID: {source.installation_id}
                    </span>
                  ) : (
                    <div className="space-y-2 pt-1">
                      <p className="text-destructive font-sans">
                        You must complete this step before you can use this source.
                      </p>
                      {source.app_slug && (
                        <Button
                          size="sm"
                          className="h-7 text-xs w-full"
                          loading={installMutation.isPending}
                          onClick={() => installMutation.mutate(source.id)}
                        >
                          <ExternalLink className="mr-1.5 h-3 w-3" aria-hidden />
                          Install Repositories on GitHub
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs w-full"
                        loading={refreshMutation.isPending}
                        onClick={() => refreshMutation.mutate(source.id)}
                      >
                        <RefreshCw className="mr-1.5 h-3 w-3" aria-hidden />
                        Refresh Installation
                      </Button>
                    </div>
                  )}
                </CardDescription>
              </CardHeader>
            </Card>
          ))}
        </div>
      )}

      <NewGitHubAppDialog open={addOpen} onClose={() => setAddOpen(false)} />
    </div>
  );
}

function NewGitHubAppDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [name, setName] = useState(generateAppName);

  const manifestMutation = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/sources/github/manifest" as any, {
        body: { name } as any,
      });
      if (error || !data) throw new Error(apiErrorMessage(error, "Failed to generate manifest"));
      return data as { manifest: Record<string, unknown> };
    },
    onSuccess: (data) => {
      const form = document.createElement("form");
      form.method = "POST";
      form.action = "https://github.com/settings/apps/new";
      form.target = "_self";

      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "manifest";
      input.value = JSON.stringify(data.manifest);

      form.appendChild(input);
      document.body.appendChild(form);
      form.submit();
      document.body.removeChild(form);
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Failed"),
  });

  return (
    <Dialog open={open} onClose={onClose} className="max-w-2xl">
      <DialogContent>
        <DialogTitle>New GitHub App</DialogTitle>
        <DialogDescription>
          This is required if you would like to get full integration (commit / pull request
          deployments, etc) with GitHub.
        </DialogDescription>

        <div className="grid gap-4 py-4 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="app-name">
              Name <span className="text-destructive">*</span>
            </Label>
            <div className="flex gap-2">
              <Input
                id="app-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-github-app"
                className="font-mono text-sm"
              />
              <Button
                type="button"
                variant="outline"
                size="icon"
                title="Generate a new random name"
                onClick={() => setName(generateAppName())}
              >
                <RefreshCw className="h-4 w-4" aria-hidden />
              </Button>
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="org-name">Organization (on GitHub)</Label>
            <Input
              id="org-name"
              placeholder="If empty, your GitHub user will be used."
              disabled
            />
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <Card className="border-primary/50 bg-primary/5">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Zap className="h-4 w-4" aria-hidden /> Automated Installation
                </div>
                <Badge className="text-[10px] uppercase">Recommended</Badge>
              </CardTitle>
              <CardDescription className="text-xs">
                Register a GitHub App via GitHub's manifest flow. Permissions and webhooks are
                pre-configured.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button
                className="w-full bg-[#6c5ce7] hover:bg-[#5b4bc4] text-white"
                loading={manifestMutation.isPending}
                onClick={() => {
                  if (!name.trim()) return toast.error("Name is required");
                  manifestMutation.mutate();
                }}
              >
                Register Now
              </Button>
            </CardContent>
          </Card>

          <Card className="opacity-50 grayscale">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm flex items-center justify-between">
                <div className="flex items-center gap-2">Manual Installation</div>
                <Badge variant="outline" className="text-[10px] uppercase">
                  Advanced
                </Badge>
              </CardTitle>
              <CardDescription className="text-xs">
                Fill the GitHub App form manually. For self-hosted GitHub Enterprise or custom
                permission setups.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button variant="outline" className="w-full" disabled>
                Continue
              </Button>
            </CardContent>
          </Card>
        </div>
      </DialogContent>
    </Dialog>
  );
}
