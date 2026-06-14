import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";

export function AdvancedSettings() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Advanced Settings</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <Label>Force Rebuild</Label>
            <p className="text-sm text-muted-foreground">Clear build cache before next deployment.</p>
          </div>
          <Button variant="outline">Enable</Button>
        </div>
        <div className="flex items-center justify-between">
          <div>
            <Label>Auto Deploy</Label>
            <p className="text-sm text-muted-foreground">Automatically deploy when new commits are pushed.</p>
          </div>
          <Button variant="outline">Enable</Button>
        </div>
      </CardContent>
    </Card>
  );
}
