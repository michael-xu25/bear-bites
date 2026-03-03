import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            MenuBrowsingView()
                .tabItem {
                    Label("Menu", systemImage: "fork.knife")
                }

            AddFavoriteView()
                .tabItem {
                    Label("Add Favorite", systemImage: "plus.circle")
                }
        }
    }
}

#Preview {
    ContentView()
}
