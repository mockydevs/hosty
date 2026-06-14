
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Copy } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { copyToClipboard } from "@/lib/utils";

export function WebhooksConfig({ stackId }: { stackId: number }) {
  const { data: info } = useQuery({
    queryKey: ["stacks", stackId, "webhook_info"],
    queryFn: async () => {
      // @ts-ignore
      const res = await api.GET(`/api/stacks/${stackId}/webhook_info`);
      return res.data;
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Incoming Webhooks</CardTitle>
        <CardDescription>Trigger deployments from external services like GitHub or GitLab.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2">
          <Label>Webhook URL</Label>
          <div className="flex gap-2">
            <Input readOnly value={info?.url || "Loading..."} />
            <Button variant="outline" size="icon" onClick={() => copyToClipboard(info?.url || "")}>
              <Copy className="w-4 h-4" />
            </Button>
          </div>
        </div>
        <div className="grid gap-2">
          <Label>Webhook Secret</Label>
          <div className="flex gap-2">
            <Input readOnly value={info?.secret || "Loading..."} />
            <Button variant="outline" size="icon" onClick={() => copyToClipboard(info?.secret || "")}>
              <Copy className="w-4 h-4" />
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
