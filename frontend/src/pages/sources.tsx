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
import { Github, Plus, Trash2, Zap } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";

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
      const { error } = await (api as any).DELETE(`/api/sources/${id}`);
      if (error) throw new Error(apiErrorMessage(error, "Failed to delete source"));
    },
    onSuccess: () => {
      toast.success("Source removed");
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Delete failed"),
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
                <CardDescription className="font-mono text-xs text-muted-foreground space-y-0.5">
                  <span className="block">App ID: {source.app_id}</span>
                  {source.installation_id ? (
                    <span className="block text-green-500">
                      Install ID: {source.installation_id}
                    </span>
                  ) : (
                    <span className="block text-destructive">
                      No installation ID — repos unavailable. Re-register the app.
                    </span>
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
  const [name, setName] = useState("hosty-integration");

  const manifestMutation = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/sources/github/manifest" as any, {});
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
            <Input
              id="app-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-github-app"
            />
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
