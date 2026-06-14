import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

export function PreviewDeploymentsConfig() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Preview Deployments</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <Label>PR Preview Deployments</Label>
            <p className="text-sm text-muted-foreground">Automatically deploy a preview environment for every pull request.</p>
          </div>
          <Button variant="outline">Enable</Button>
        </div>
      </CardContent>
    </Card>
  );
}
