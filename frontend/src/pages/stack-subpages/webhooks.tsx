import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";

export function WebhooksConfig() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Webhooks</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-2">
          <Label>Deployment Webhook URL</Label>
          <div className="flex gap-2">
            <Input readOnly value="https://api.hosty.dev/hooks/deploy/xyz123" className="font-mono text-xs" />
            <Button variant="secondary">Regenerate</Button>
          </div>
          <p className="text-xs text-muted-foreground">Send a POST request to this URL to trigger a new deployment manually.</p>
        </div>
      </CardContent>
    </Card>
  );
}
