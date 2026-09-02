package ai.hassan.fixture;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

public final class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        TextView view = new TextView(this);
        view.setText("Hassan cloud Android build verified");
        view.setTextSize(20);
        view.setPadding(32, 32, 32, 32);
        setContentView(view);
    }
}
